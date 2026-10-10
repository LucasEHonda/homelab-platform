# Shared data services: operator runbook

Moves games, pinguei and financas from their own database containers to the shared `data-postgres` and `data-redis`. Do the apps one at a time, in this order: games, pinguei, financas.

**Warning:** between `prepare` and `finish` of an app, no other push to that app's `main` may happen. A deploy in that window starts the app against a half-migrated database. Merge only the migration PR.

All commands run from this repository; set `NAS_HOST=user@host` (and `DEPLOYER_VERSION` for `nas-setup`).

## 1. Platform

1. Release the new deployer version (tag this repository).
2. `make nas-setup DEPLOYER_VERSION=vX.Y.Z`. This creates the data services (Postgres, Redis, backup).
3. For each app: `make nas-provision APP=games`, then `pinguei`, then `financas`. This creates the database, the roles, `/mnt/svd/platform/.envs/.data-<app>` and the Redis user. It is safe to re-run. Read the read-only password with the `sudo grep READONLY_PASSWORD ...` line it prints.

## 2. Per app: games, then pinguei

1. `make nas-migrate APP=<app> STEP=prepare`. It checks the target, asks for an Infisical admin token, writes the new Infisical values, stops the app, and saves a dump at `/mnt/svd/<app>/backups/pre-migration-<timestamp>.sql.gz`. The app is down from here.
2. Merge the app's `chore/shared-postgres` PR. Wait for the deploy to finish; the new release creates the empty schema through Django migrations.
3. `make nas-migrate APP=<app> STEP=load`. It copies the data with pgloader, runs `verify`, and starts the app again. If `verify` fails the app stays stopped. `load` and `verify` stop the old MySQL container and read its data dir through a temporary MySQL that allows the login method pgloader needs (`mysql_native_password`); the old container is kept, so starting it again is the rollback.
4. Smoke test the app.
5. `make nas-migrate APP=<app> STEP=finish`. The old MySQL data stays at `/mnt/svd/<app>/data/mysql`; delete it after 30 days.

`make nas-migrate APP=<app> STEP=verify` can be run at any time after `prepare` to compare old and new again.

## 3. Per app: financas

1. `make nas-migrate APP=financas STEP=prepare`. Same as above, and it also restores the dump into the shared database and runs `verify`.
2. Merge the `chore/shared-data` PR. Wait for the deploy to finish.
3. `make nas-migrate APP=financas STEP=finish`. The old Postgres data stays at `/mnt/svd/financas/data/postgres`; delete it after 30 days.

## Rollback

The old container data is never touched, so rolling back is always possible until you delete it.

1. Revert the app's migration PR on `main` and let it deploy: the app goes back to its own database container.
2. The old database still holds the data from before `prepare`. Writes made after the switch are only in the shared database.
3. If the old database must be rebuilt, restore `/mnt/svd/<app>/backups/pre-migration-<timestamp>.*` into it.
4. To retry, empty the shared database and start from `prepare` again:
   `sudo docker exec platform-data-postgres-1 psql -U platform -d <app> -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION <app>'`, then `sudo rm -rf /mnt/svd/platform/data/migrations/<app>`, then `make nas-provision APP=<app>` to restore the privileges and extension.
