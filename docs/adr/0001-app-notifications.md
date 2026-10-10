# ADR 0001: Push notifications for the apps

Status: Proposed
Date: 2026-10-10

## Context

Three apps run on a TrueNAS NAS: games (Django and React), pinguei (a support widget) and financas (Django and TanStack Start). Financas is a PWA with a manifest only. It has no service worker. Each app is reachable only through its own Tailscale node. There is no public ingress. The apps have outbound internet access through an `egress` docker network. The deployer already sends deploy results to public ntfy.sh. It uses a secret 32-hex topic, no auth, and a plain httpx POST (`deployer/notify.py`). That module already defines a small `Notifier` protocol with one `send(message)` method. No app has a notification feature today. The owner asked whether the apps can reuse the ntfy approach, and how to get free push that scales. The owner is concerned that Web Push on iOS works only when the site is installed to the Home Screen.

## Options

### Web Push (VAPID)

How it works: the browser registers a service worker and subscribes through the platform push service (FCM, Mozilla, Apple). The app stores the subscription per user. The backend signs a request with its VAPID key and posts an encrypted payload to the push service. The push service wakes the service worker, which shows the notification. The backend only needs outbound internet, so this fits the `egress` network. The user's device does not need to reach the NAS at delivery time.

- Cost: free. No account is needed with the push services.
- Scale limits: none that matter here. The backend sends one POST per subscription. Expired subscriptions (HTTP 404 or 410) must be deleted.
- iOS: works only for a web app added to the Home Screen, on iOS and iPadOS 16.4 or later. The permission prompt must come from a user gesture inside the installed app. No Apple Developer account is needed. Source: https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/
- Android: works in Chrome and other browsers, installed or not.
- User must install: on iOS, the PWA. On Android and desktop, nothing.
- Security and privacy: the payload is encrypted end to end between backend and browser. The push service sees only metadata. Subscription endpoints are secret URLs and must be stored as sensitive data. Auth is the VAPID key held by the backend.
- Effort: M. Backend: subscription model, VAPID keys, a sender adapter (for example pywebpush), cleanup of dead subscriptions. Frontend: service worker, permission button, subscribe call. Financas also needs the service worker added. Games and pinguei need a manifest and a service worker.
- Works with Tailscale-only apps: yes. Delivery goes backend to push service to device, and needs no inbound path. Tapping a notification opens the app URL, which needs Tailscale on the device. Service workers also need a secure context, so the Tailscale HTTPS name must be used.

### ntfy.sh with per-user topics

How it works: the backend POSTs to `https://ntfy.sh/<topic>`, as the deployer does. Each user gets a random unguessable topic and subscribes in the ntfy app. The message can carry a click URL back to the app.

- Cost: free tier. Messages over 4096 bytes become attachments, and attachments are limited to 15 MB each, 100 MB per visitor, and kept 3 hours. Source: https://docs.ntfy.sh/publish/#limitations. The request rate and daily message limits were not confirmed in this review. Check them before relying on this for many users.
- Scale limits: the free tier is shared and rate limited per IP. All apps publish from the same NAS IP. This is the main limit.
- iOS: the ntfy iOS app delivers instantly for ntfy.sh topics. No PWA install is needed.
- Android: the ntfy Android app works, with or without Google services.
- User must install: the ntfy app, and subscribe to the topic by hand or by link.
- Security and privacy: without auth, anyone who knows the topic can read and publish. The topic is the only secret. Messages pass through a third party in plain text. Use 128-bit random topics and keep message text non-sensitive.
- Effort: S to M. Backend: topic column per user, a sender adapter, a settings screen that shows the topic and a subscribe link. Frontend: one settings page.
- Works with Tailscale-only apps: yes for sending, because it is outbound only. The ntfy app does not need Tailscale. The click link does.

### Self-hosted ntfy

How it works: run an ntfy server on the NAS. Apps publish to it on the internal network. Devices subscribe to it with per-user access tokens and ACLs.

- Cost: free, plus the NAS resources.
- Scale limits: bound by the NAS. Fine for a few users.
- iOS: iOS cannot keep a background connection, so the server needs `upstream-base-url: https://ntfy.sh`. The server then sends a poll request containing only the message ID and a hash of the topic URL. Apple wakes the app, and the app fetches the real message from the self-hosted server. Without the upstream, delivery can take hours. Source: https://docs.ntfy.sh/config/#ios-instant-notifications
- Android: works. The app can hold its own connection to the server.
- User must install: the ntfy app, and enter the server URL and token.
- Security and privacy: full control. Messages stay on the NAS. Auth and ACLs are supported. But devices must reach the server. That means either public exposure, or Tailscale on every phone. With Tailscale only, the iOS app can fetch messages while the tailnet is up, but the wake-up path still goes through ntfy.sh.
- Effort: M. One more service to deploy and back up, user and token provisioning, plus the same app-side pieces as the ntfy.sh option.
- Works with Tailscale-only apps: partly. It works if every user device runs Tailscale. It breaks for users without Tailscale, unless the server is exposed publicly.

### Telegram bot

How it works: the user starts a chat with the bot, and the app links the chat ID to the user, usually through a one-time code. The backend calls the Bot API `sendMessage` over HTTPS.

- Cost: free. The Bot API has per-chat and global send rate limits (roughly one message per second per chat, about 30 per second overall). These figures come from general knowledge, and the Telegram FAQ page could not be fetched to confirm them. Source to check: https://core.telegram.org/bots/faq
- Scale limits: rate limits are far above what these apps need.
- iOS: works with the Telegram app. No PWA install.
- Android: works with the Telegram app.
- User must install: Telegram, and a Telegram account.
- Security and privacy: Telegram can read bot messages. Bot chats are not end-to-end encrypted. The bot token must be kept secret. Anyone who gets a link code could bind their chat, so codes must be single use and short lived.
- Effort: M. Backend: bot token, link flow with webhook or polling, a sender adapter. Frontend: a "connect Telegram" button. With a Tailscale-only app, the link flow must use long polling, because Telegram cannot reach a webhook.
- Works with Tailscale-only apps: yes, with long polling for the link step. Sending is outbound only.

### Email

How it works: the backend sends mail through SMTP or a transactional provider. Django already supports this.

- Cost: free at low volume with many providers. Deliverability needs SPF and DKIM on a sending domain, which may cost a domain.
- Scale limits: provider quotas. Fine for these apps.
- iOS: works with the Mail app. Delivery depends on the mail app's fetch or push settings, and is often not instant.
- Android: works with any mail app.
- User must install: nothing.
- Security and privacy: mail is plain text across servers and stays in mailboxes. Financial values do not belong in it. Auth is the user's own mailbox.
- Effort: S. Backend: a mail adapter and templates. Frontend: a toggle.
- Works with Tailscale-only apps: yes. Sending is outbound only. Links back to the app need Tailscale on the reading device.

## The iOS question

Web Push on iOS needs the PWA added to the Home Screen, on iOS or iPadOS 16.4 or later. The permission prompt must be triggered by a user gesture, such as a tap on a "Enable notifications" button, inside the installed app. A plain Safari tab cannot subscribe. This is a platform rule, and it will not change from our side. Source: https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/

Alternatives that work on iOS without installing the PWA:

- The ntfy iOS app. Delivery is instant for ntfy.sh topics. A self-hosted server needs `upstream-base-url` pointing to ntfy.sh to wake iOS. Source: https://docs.ntfy.sh/config/#ios-instant-notifications
- Telegram, through the Telegram app.
- Email, through the Mail app, with less reliable timing.

A native iOS app would need an Apple Developer account, which is paid. It is out of scope.

## Comparison

| Option | Cost | iOS without install | Scales to many users | Setup for end user | Effort | Privacy |
|---|---|---|---|---|---|---|
| Web Push (VAPID) | Free | No, PWA must be on Home Screen | Yes | Install PWA on iOS; one tap on Android | M | Good: encrypted payload |
| ntfy.sh per-user topics | Free tier | Yes, with ntfy app | Limited by shared rate limits | Install ntfy app, subscribe | S to M | Weak: third party sees text, topic is the only secret |
| Self-hosted ntfy | Free, NAS resources | Yes, needs upstream to ntfy.sh | Limited by NAS | Install ntfy app, set server and token; Tailscale or public exposure | M | Good: stays on NAS, but exposure needed |
| Telegram bot | Free | Yes, with Telegram app | Yes | Telegram account, link chat | M | Weak: Telegram can read messages |
| Email | Free at low volume | Yes, with Mail app | Yes | None | S | Weak: plain text in mailboxes |

## Recommendation

Build a small notification port in each app. It is a `Notifier` interface with one adapter per channel, plus a per-user `NotificationPreference` row that chooses the channels. Business code calls the port with an event and a user. It never knows which channel carries the message. New channels can then be added without touching business code. The deployer already uses the same shape in `deployer/notify.py`.

The first channel is Web Push. It is free and it is a standard. No third party sees the content beyond the encrypted payload. It scales with the number of users at no cost, and it needs only outbound internet from the apps.

The second channel is for iOS users who do not install the PWA. If public exposure of a server is acceptable, use a self-hosted ntfy with a per-user random topic and an access token, with `upstream-base-url` set to ntfy.sh. If it is not acceptable, use ntfy.sh with a per-user secret topic and non-sensitive message text. That means no amounts and no names.

Financial values never go in ntfy or Telegram messages. The message states that something happened and links back to the app, where the user sees the detail after authenticating.

## Candidate events

- financas: bill due tomorrow, card statement processed, statement import failed, income settled.
- games: library sync finished, sync failed.
- pinguei: new unread support message for agents.

## Consequences

- Each app gets a notification module, a preference table and a settings screen. The three apps repeat this work unless the port is extracted into a shared package later.
- Financas, games and pinguei need a service worker and a manifest to support Web Push. Financas has only the manifest today.
- Push subscriptions and ntfy topics become sensitive data. They need protection at rest and removal when a user is deleted.
- iOS users who skip the PWA install get a weaker channel with limited message text.
- Reliance on ntfy.sh means a third party outage or rate limit can drop messages. Failures must be logged and must never block the business action, as `NtfyNotifier` does today.
- Tapping a notification opens the app, so the device still needs Tailscale to see the detail.

## Next steps

1. Decide whether public exposure of a self-hosted ntfy is acceptable.
2. Confirm the current ntfy.sh request and daily message limits for the shared NAS IP.
3. Define the `Notifier` interface, the event types and the `NotificationPreference` model.
4. Implement the Web Push adapter, service worker and settings screen in financas first.
5. Add the ntfy adapter and the per-user topic flow.
6. Wire the candidate events in each app, with non-sensitive message text.
7. Port the same module to games and pinguei.
