# Shared data services: migration record

The move of games, pinguei and financas from their own database containers to the shared `data-postgres` and `data-redis` finished on 2026-10-11. This page records how it was done. The one-off script and its `make nas-migrate` target were removed afterwards; read the script with `git show be4005c:scripts/nas-migrate-app-db.sh`.

## What runs now

- `data-postgres` (Postgres 16): one database and one role per app (`games`, `pinguei`, `financas`), plus `<app>_readonly` and `<app>_breakglass`. CONNECT is revoked from every other role.
- `data-redis` (Redis 7.4): one ACL user per app. games uses DB 0; financas uses DB 1 (cache), 2 (broker) and 3 (results); pinguei uses no Redis.
- `data-backup`: a nightly `pg_dump` of each database at 03:30 into `/mnt/svd/<app>/backups/`, kept 14 days.
- A new app gets its database and Redis user with `make nas-provision NAS_HOST=user@host APP=<app>`.

## How each app was moved

1. Platform: tag `v0.2.0`, `make nas-setup`, then `make nas-provision` for the three apps.
2. games and pinguei (MySQL 8): `prepare` wrote the new Infisical values, stopped the app and dumped MySQL. Merging the app's PR deployed the release whose migrations created the schema. `load` copied the data with pgloader 3.6.7 from a temporary MySQL started with `mysql_native_password`, then compared the row count and a primary-key hash of every table. `finish` checked the containers.
3. financas (Postgres 16): `prepare` restored a `pg_dump` into the shared database and compared a whole-row md5 of every table (36 tables, all equal). Merging the PR deployed it, and `finish` checked the containers.
4. After the checks, the old database data dirs and the pre-migration dumps were deleted.

## Problems found, for the next migration

- The deployer writes env files as `KEY='value'`; scripts that read them must strip the quotes.
- An `apps.yml` written before `shared_networks` existed blocks any app that joins `platform-data`; `nas-setup` now adds the key.
- pgloader 3.6.7 cannot log in with MySQL 8's default `caching_sha2_password`.
- Django stores a UUIDField as 32 hex chars on MySQL, while Postgres prints `uuid` with hyphens; compare keys without hyphens.
- Every production compose must load `.postgres` in the app containers. pinguei did not, and its first deploy rolled back.
- pgloader did not reset the sequences of pinguei; `manage.py sqlsequencereset` fixed them.
- Capping Infisical's Node heap at 384 MB made it crash at startup; the cap was reverted.
