#!/bin/sh
# Creates (or repairs) one app's database, roles, credentials file and Redis ACL user on the shared data services.
# Run as root; `make nas-provision APP=<app>` copies it over and runs it:
#
#   nas-provision-app-data.sh <games|pinguei|financas>
#
# Env: PLATFORM_DIR (default /mnt/svd/platform). Every step is idempotent: re-running rotates nothing.
set -eu

APP=${1:?usage: nas-provision-app-data.sh <games|pinguei|financas>}
P=${PLATFORM_DIR:-/mnt/svd/platform}
PG=platform-data-postgres-1
REDIS=platform-data-redis-1

case "$APP" in
  games | pinguei | financas) ;;
  *) echo "nas-provision-app-data: unknown app '$APP'" >&2; exit 64 ;;
esac

[ "$(id -u)" -eq 0 ] || { echo "nas-provision-app-data: run as root (sudo $0 ...)" >&2; exit 77; }

log() { echo "==> $*"; }
warn() { echo "nas-provision-app-data: $*" >&2; }
die() { warn "$*"; exit 1; }
random_secret() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
# The deployer writes KEY='value'; files written by these scripts have no quotes.
env_value() { sed -n "s/^$1=//p" "$2" | tail -1 | sed "s/^'\(.*\)'\$/\1/"; }

# write_file <path>: writes stdin to <path> with mode 0600 from the first byte.
write_file() {
  python3 -c '
import os, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as out:
    out.write(sys.stdin.read())
' "$1"
}

docker exec "$PG" true 2>/dev/null || die "$PG is not running; run make nas-setup first"

# 1. Credentials file. Never regenerated, so a re-run keeps the passwords the apps already use.
credentials="$P/.envs/.data-$APP"
if [ ! -f "$credentials" ]; then
  if [ "$APP" = financas ]; then
    # Reusing the current password keeps the old secret valid until the app is switched over.
    postgres_password=$(env_value POSTGRES_PASSWORD /mnt/svd/financas/.envs/.production/.postgres)
    [ -n "$postgres_password" ] || die "no POSTGRES_PASSWORD in /mnt/svd/financas/.envs/.production/.postgres"
  else
    postgres_password=$(random_secret 32)
  fi
  log "writing $credentials"
  printf 'POSTGRES_PASSWORD=%s\nREDIS_PASSWORD=%s\nREADONLY_PASSWORD=%s\nBREAKGLASS_PASSWORD=%s\n' \
    "$postgres_password" "$(random_secret 32)" "$(random_secret 32)" "$(random_secret 32)" \
    | write_file "$credentials"
fi
postgres_password=$(env_value POSTGRES_PASSWORD "$credentials")
redis_password=$(env_value REDIS_PASSWORD "$credentials")
readonly_password=$(env_value READONLY_PASSWORD "$credentials")
breakglass_password=$(env_value BREAKGLASS_PASSWORD "$credentials")

# 2. Roles and database. Passwords travel as psql variables (:'name'), never inside the SQL text.
log "roles and database for $APP"
docker exec -i "$PG" psql -X -q -v ON_ERROR_STOP=1 -U platform -d postgres \
  -v app="$APP" -v ro="${APP}_readonly" -v bg="${APP}_breakglass" \
  -v pw="$postgres_password" -v ropw="$readonly_password" -v bgpw="$breakglass_password" <<'SQL'
SELECT format('CREATE ROLE %I', :'app') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app') \gexec
SELECT format('CREATE ROLE %I', :'ro') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro') \gexec
SELECT format('CREATE ROLE %I', :'bg') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'bg') \gexec
ALTER ROLE :"app" LOGIN PASSWORD :'pw';
ALTER ROLE :"ro" LOGIN PASSWORD :'ropw';
ALTER ROLE :"bg" NOLOGIN PASSWORD :'bgpw';
SELECT format('CREATE DATABASE %I OWNER %I', :'app', :'app') WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'app') \gexec
REVOKE CONNECT ON DATABASE :"app" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"app" TO :"app", :"ro", :"bg";
SQL

# 3. Extension and privileges inside the app database. The default privileges cover tables the app
# creates later (Django migrations, pg_restore --role), so the grants survive schema changes.
log "extension and privileges in database $APP"
docker exec -i "$PG" psql -X -q -v ON_ERROR_STOP=1 -U platform -d "$APP" \
  -v app="$APP" -v ro="${APP}_readonly" -v bg="${APP}_breakglass" <<'SQL'
CREATE EXTENSION IF NOT EXISTS unaccent;
GRANT USAGE ON SCHEMA public TO :"ro";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"ro";
ALTER DEFAULT PRIVILEGES FOR ROLE :"app" IN SCHEMA public GRANT SELECT ON TABLES TO :"ro";
GRANT ALL ON SCHEMA public TO :"bg";
GRANT ALL ON ALL TABLES IN SCHEMA public TO :"bg";
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO :"bg";
ALTER DEFAULT PRIVILEGES FOR ROLE :"app" IN SCHEMA public GRANT ALL ON TABLES TO :"bg";
ALTER DEFAULT PRIVILEGES FOR ROLE :"app" IN SCHEMA public GRANT ALL ON SEQUENCES TO :"bg";
SQL

# 4. Redis ACL user (pinguei uses no Redis). Only the password hash is stored in users.acl.
if [ "$APP" != pinguei ]; then
  acl="$P/data/data-redis/users.acl"
  [ -f "$acl" ] || die "$acl not found; run make nas-setup first"
  log "Redis ACL user $APP"
  hash=$(printf '%s' "$redis_password" | sha256sum | cut -d' ' -f1)
  tmp=$(mktemp)
  (grep -v "^user $APP " "$acl" || true) > "$tmp"
  printf 'user %s on #%s ~* &* +@all -@admin -flushall -flushdb\n' "$APP" "$hash" >> "$tmp"
  # cat keeps the owner and mode of the file Redis already reads.
  cat "$tmp" > "$acl"
  rm -f "$tmp"
  REDIS_ADMIN_PASSWORD=$(env_value REDIS_ADMIN_PASSWORD "$P/.envs/.data-redis")
  [ -n "$REDIS_ADMIN_PASSWORD" ] || die "no REDIS_ADMIN_PASSWORD in $P/.envs/.data-redis"
  docker exec "$REDIS" redis-cli --user platform --pass "$REDIS_ADMIN_PASSWORD" ACL LOAD >/dev/null
fi

log "database $APP ready: roles $APP, ${APP}_readonly, ${APP}_breakglass (no login until opened)"
echo "readonly password: sudo grep READONLY_PASSWORD $credentials"
