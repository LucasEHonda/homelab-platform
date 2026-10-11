# homelab-platform

This repository provides automatic, push-triggered deploys of self-hosted apps on a TrueNAS box that is reachable only through Tailscale. Secrets are kept in a self-hosted Infisical instance.

## How a deploy works

1. The app's release workflow publishes its images and a deploy bundle, then pushes the release tag.
2. The deploy job of that workflow calls `.github/workflows/deploy.yml` in this repository, pinned by commit SHA.
3. The GitHub Actions runner joins the tailnet as an ephemeral `tag:ci` node.
4. The runner sends a GitHub OIDC token (audience `homelab-deployer`) and the image digests to the deployer.
5. The deployer verifies the token (repository, repository id, ref, and workflow).
6. The deployer pulls the deploy bundle by digest and checks the compose policy.
7. The deployer fetches the app's secrets from Infisical.
8. The deployer backs up the database.
9. The deployer switches `<app dir>/deploy` to the new release and runs compose.
10. The deployer waits for the containers to become healthy. If migrations fail, it rolls back.
11. The deployer posts the result to ntfy.

## Security model

- No long-lived credential outside the NAS can open a shell on it.
- A leaked Tailscale OAuth client only reaches the deployer endpoint. It still needs a valid OIDC token from an allowed repository and workflow.
- A leaked GHCR token cannot deploy, because the deployer only runs digests sent by an authenticated workflow.
- The compose policy rejects bind mounts, devices, configs and secrets outside the app directory, privileged mode, host namespaces, the Docker socket, and builds. This keeps the rest of the NAS, including the photos, out of reach.
- The deployer itself holds the Docker socket, so it is the one component that must be protected.
- ntfy messages carry only the app, the version and the status. When a deploy is blocked, they also list the missing secret names.
- Apps pin this workflow by SHA, so a change here reaches an app only through a reviewed bump.

## One-time setup

Only steps 1 and 4 need a browser; the rest is scripted.

1. **Tailscale admin console** (https://login.tailscale.com/admin):
   - Access controls: merge [`tailscale/policy.hujson`](tailscale/policy.hujson) into the policy file. Make sure no grant lets `*` or `autogroup:member` reach every node; admins reaching `*` is fine (that also covers DBeaver, Infisical and break-glass). Add each app's own `deny` targets to the `tests` block.
   - Settings → Keys: create three auth keys (not reusable, not ephemeral), tagged `tag:deployer`, `tag:secrets` and
     `tag:observe`.
   - Settings → Trust credentials: if workload identity federation is offered, create one for GitHub
     (issuer `https://token.actions.githubusercontent.com`, subject `repo:<owner>/<repo>:*` per app, tag
     `tag:ci`) and note its client ID and audience. Otherwise create an OAuth client with the `auth_keys`
     scope (write) and tag `tag:ci`, and note its client ID and secret.
2. **GitHub**: `scripts/github-setup.sh my-games-hub-br/my-games-hub` (add the other app repositories when they
   move over). It asks for the tailnet name and the Tailscale client and sets `DEPLOYER_URL`,
   `TS_OAUTH_CLIENT_ID` and `TS_OAUTH_SECRET` (or `TS_AUDIENCE`) on each repository. GitHub Free cannot share
   organization secrets with private repositories, so they are set per repository.
3. **NAS**: `make nas-setup NAS_HOST=user@host`. It asks for the SSH and sudo passwords, then only what it cannot
   generate: tailnet name, your email, the three auth keys, a GitHub user and a classic token with only
   `read:packages` (the deployer's own registry login), and a password for your new Infisical account. It then:
   creates `/mnt/svd/platform`, generates every other secret, starts Infisical, creates your admin account, writes the OpenObserve root login and starts OpenObserve, one
   Infisical project per app with the app's current `.envs/.production/*` imported as folders,
   a read-only machine identity per app
   whose credentials go to `.envs/.deployer`, deletes the old `<app> auto-update` cron jobs, and starts the
   deployer. Safe to re-run; `DEPLOYER_VERSION=vX.Y.Z` picks the deployer release (default: latest tag).
4. **Phone**: install the ntfy app and subscribe to the URL that `nas-setup` printed.
5. Keep an offline copy of Infisical's `ENCRYPTION_KEY` (the command is printed at the end of `nas-setup`).

## Adding an app

- add its tag and grants to `tailscale/policy.hujson` and the tailnet policy;
- add a bind of its app dir (same path on both sides) to the `deployer` service in `deploy/compose.yml`;
- run `make nas-setup NAS_HOST=user@host` after adding the app to `APPS` (and its generated keys to `GENERATE`) in the Makefile; it appends the app to `deploy/apps.yml` on the NAS and, because Infisical already exists, asks for a short-lived admin token (Infisical -> Organization -> Access Control -> Identities -> Instance Admin Identity -> Token Auth -> Create token, TTL 1h) to create the app's project and read-only identity;
- `scripts/github-setup.sh <owner>/<repo>`;
- in the app: `deploy/deploy.yml`, `deploy/Dockerfile.bundle`, and a `deploy.yml` workflow calling this repo's workflow by SHA (copy my-games-hub).

## Adding a secret to an app

A key added to an app's `.envs/.production/*.example` blocks the deploy until it exists in the app's Infisical project. Before merging the change, run:

```bash
make secrets APP=<app> REF=<branch> INFISICAL_URL=https://secrets.<tailnet>.ts.net
```

It reads the templates of that branch, then for each key the project lacks it generates the value or asks for it, and creates it. An existing secret is never changed. It runs on your machine over Tailscale (no SSH) and asks for a short-lived Infisical admin token (as above). `CHECK=1` only lists the missing keys.

A template asks for a generated value with a comment right above the key. Keys without one are asked for, with the template value as the default unless it is an `__PLACEHOLDER__`.

```
# generate: token_urlsafe 50      (also token_hex N; N bytes, 16-128)
DJANGO_SECRET_KEY=
# generate: vapid_public_key      (Web Push; the two halves are generated as one pair)
VAPID_PUBLIC_KEY=
# generate: vapid_private_key
VAPID_PRIVATE_KEY=
```

## Rotating credentials

- Tailscale OAuth client: create a new one, update the organization secrets, then delete the old one.
- Infisical machine identity client secret: create a new one, update `.envs/.deployer`, restart the deployer, then revoke the old one.
- ntfy topic: choose a new random topic, update the environment, then resubscribe in the app.
- `ENCRYPTION_KEY` is never rotated by hand.

## Database access

- DBeaver connects to `<app>.<tailnet>:3306` (port 5432 for Postgres apps) as `readonly`.
- The `breakglass` user is locked.
- An admin device can unlock it for one hour with `curl -X POST https://deployer.<tailnet>/v1/apps/<app>/break-glass`. This also notifies ntfy.
- It is locked again after one hour, and on every deployer restart.

## Emergency lockdown

Use it when a GitHub, Tailscale or registry credential may have leaked, or when anything looks odd in a deploy.

`make lockdown NAS_HOST=user@host` cuts the deploy path in three layers that an attacker controlling GitHub or the deployer cannot undo: it stops the deployer and both Tailscale sidecars on the NAS (the apps keep running) and leaves a `LOCKDOWN` marker so `nas-setup` refuses to restart them by accident, then disables the deploy workflow and deletes the Tailscale credential secret in every app repo.

Next, revoke the Tailscale trust credential in the admin console and rotate whatever leaked (see Rotating credentials).

When it is safe, `make unlock NAS_HOST=user@host` brings the NAS back, enables the workflows and asks for a new Tailscale credential through `scripts/github-setup.sh`.

## Development

- `make test` runs the test suite in Docker.

## Shared data services

All apps use the shared Postgres (`data-postgres`) and Redis (`data-redis`), with one database, role and Redis user per app. A new app gets them with `make nas-provision NAS_HOST=user@host APP=<app>`. How games, pinguei and financas were moved there on 2026-10-11 is recorded in [`docs/shared-data-runbook.md`](docs/shared-data-runbook.md).

## Request observability

OpenObserve (`openobserve`) stores the request traces that the apps send over the internal network `platform-telemetry`, and deletes them after 30 days. Its UI is at `https://observe.<tailnet>.ts.net`, for tailnet admins only. The root login is in `/mnt/svd/platform/.envs/.openobserve`. The data is not backed up. Design: [ADR 0002](docs/adr/0002-request-observability.md). Plan and per-app steps: [docs/observability-plan.md](docs/observability-plan.md).

The apps send traces with an ingestion passcode, which can write data but cannot read it. Create it once, after the first `nas-setup` with OpenObserve:

1. In the UI, add a user such as `ingest@<your domain>` (the open source edition only offers the admin role).
2. Read its passcode: `curl -u 'ingest@<your domain>:<its password>' https://observe.<tailnet>.ts.net/api/default/passcode`.
3. Build the header value: `printf 'Authorization=Basic%%20%s\n' "$(printf '%s' 'ingest@<your domain>:<passcode>' | base64 -w0)"`.
4. In each traced app's Infisical project (environment `prod`, folder `django`), set `OTEL_EXPORTER_OTLP_TRACES_HEADERS` to that value and `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` to `http://openobserve:5080/api/default/v1/traces`, before the app's first release with tracing.

Run `make nas-setup` before an app's first release with tracing: it creates `platform-telemetry` and adds it to `shared_networks`, without which the deployer rejects the app.

OpenObserve also runs five alerts. Their definitions live in `deploy/openobserve/alerts.yml`. The deployer tool `deployer/tools/openobserve_alerts.py` applies that file. `scripts/nas-setup.sh` runs it as step 10 at the end of every setup. Each run creates or updates the alerts and deletes the ones tagged `managed:homelab-platform` that are no longer in the file. Alerts made by hand in the UI are never deleted. The alerts post to the deployer's `NTFY_URL`, the same ntfy topic as the deploy results. Their messages have the title `OpenObserve alert`. The body holds the alert name and one line per matching row. The alerts read the traces of all apps, so a newly traced app is covered with no change.

- `server-errors-5xx`: any 5xx response in 5 minutes.
- `slow-requests`: a route with at least 5 requests and a p95 above 1 second in 15 minutes.
- `many-401-403`: 20 or more 401 or 403 responses on one route in 5 minutes.
- `n-plus-one-queries`: one request ran the same SQL 10 or more times in the last hour.
- `service-silent`: an app sent spans in the last hour but none in the last 15 minutes.

To change an alert, edit `deploy/openobserve/alerts.yml`, merge the change, and run `make nas-setup NAS_HOST=user@host` with the new deployer version (`DEPLOYER_VERSION=vX.Y.Z`). Alert names must be lowercase words joined by `-`, because OpenObserve rejects spaces and symbols.
