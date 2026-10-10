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
   - Settings → Keys: create two auth keys (not reusable, not ephemeral), one tagged `tag:deployer`, one
     `tag:secrets`.
   - Settings → Trust credentials: if workload identity federation is offered, create one for GitHub
     (issuer `https://token.actions.githubusercontent.com`, subject `repo:<owner>/<repo>:*` per app, tag
     `tag:ci`) and note its client ID and audience. Otherwise create an OAuth client with the `auth_keys`
     scope (write) and tag `tag:ci`, and note its client ID and secret.
2. **GitHub**: `scripts/github-setup.sh my-games-hub-br/my-games-hub` (add the other app repositories when they
   move over). It asks for the tailnet name and the Tailscale client and sets `DEPLOYER_URL`,
   `TS_OAUTH_CLIENT_ID` and `TS_OAUTH_SECRET` (or `TS_AUDIENCE`) on each repository. GitHub Free cannot share
   organization secrets with private repositories, so they are set per repository.
3. **NAS**: `make nas-setup NAS_HOST=user@host`. It asks for the SSH and sudo passwords, then only what it cannot
   generate: tailnet name, your email, the two auth keys, a GitHub user and a classic token with only
   `read:packages` (the deployer's own registry login), and a password for your new Infisical account. It then:
   creates `/mnt/svd/platform`, generates every other secret, starts Infisical, creates your admin account, one
   Infisical project per app with the app's current `.envs/.production/*` imported as folders (plus generated
   `MYSQL_READONLY_PASSWORD` and `MYSQL_BREAKGLASS_PASSWORD` for games), a read-only machine identity per app
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

Apps move from their own database containers to a shared Postgres and Redis. The operator sequence, the per-app migration steps and the rollback are in [`docs/shared-data-runbook.md`](docs/shared-data-runbook.md).
