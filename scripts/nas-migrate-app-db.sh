#!/bin/sh
# Moves one app's data from its own database container to the shared Postgres, in steps the operator runs in order.
# Run as root; `make nas-migrate APP=<app> STEP=<step>` copies it over and runs it:
#
#   nas-migrate-app-db.sh <games|pinguei|financas> <prepare|load|verify|finish>
#
# prepare: checks, Infisical values, stops the app, takes a dump (financas: also restores it), then waits for the app PR.
# load:    games/pinguei only, after the new release created the schema: copies the MySQL data with pgloader.
# verify:  row counts (and content hashes) old vs new; exit 1 on any difference.
# finish:  checks the new containers and closes the migration.
# Steps refuse to run out of order (marker files) and are idempotent.
# Env: PLATFORM_DIR (default /mnt/svd/platform).
set -eu

APP=${1:?usage: nas-migrate-app-db.sh <games|pinguei|financas> <prepare|load|verify|finish>}
STEP=${2:?usage: nas-migrate-app-db.sh <games|pinguei|financas> <prepare|load|verify|finish>}
P=${PLATFORM_DIR:-/mnt/svd/platform}
A=/mnt/svd/$APP
M=$P/data/migrations/$APP
PG=platform-data-postgres-1
TEMP_MYSQL=migrate-$APP-mysql
BT='`'

case "$APP" in
  games | pinguei | financas) ;;
  *) echo "nas-migrate-app-db: unknown app '$APP'" >&2; exit 64 ;;
esac

[ "$(id -u)" -eq 0 ] || { echo "nas-migrate-app-db: run as root (sudo $0 ...)" >&2; exit 77; }

log() { echo "==> $*"; }
warn() { echo "nas-migrate-app-db: $*" >&2; }
die() { warn "$*"; exit 1; }
# The deployer writes KEY='value'; files written by these scripts have no quotes.
env_value() { sed -n "s/^$1=//p" "$2" | tail -1 | sed "s/^'\(.*\)'\$/\1/"; }
marked() { [ -f "$M/$1" ]; }
mark() { date -u +%Y-%m-%dT%H:%M:%SZ > "$M/$1"; }

notify() {
  url=$(sed -n 's/^NTFY_URL=//p' "$P/.envs/.deployer" 2>/dev/null | tail -1)
  [ -n "$url" ] || return 0
  curl -fsS -m 10 -d "$1" "$url" >/dev/null 2>&1 || python3 -c 'import sys,urllib.request; urllib.request.urlopen(urllib.request.Request(sys.argv[1], data=sys.argv[2].encode()), timeout=10)' "$url" "$1" || true
}

ask() {
  # ask <variable> <prompt> [secret]
  printf '%s: ' "$2" >/dev/tty
  if [ "${3:-}" = secret ]; then stty -echo </dev/tty; fi
  IFS= read -r answer </dev/tty
  if [ "${3:-}" = secret ]; then stty echo </dev/tty; echo >/dev/tty; fi
  eval "$1=\$answer"
}

ask_required() {
  # ask_required <variable> <prompt> [secret]: asks again until the answer is not empty.
  while :; do
    ask "$@"
    eval "[ -n \"\$$1\" ]" && return 0
    echo "  required" >/dev/tty
  done
}

mkdir -p "$M"
DATA_ENV=$P/.envs/.data-$APP
[ -f "$DATA_ENV" ] || die "run make nas-provision APP=$APP first"
docker exec "$PG" true 2>/dev/null || die "$PG is not running"
NEW_PASSWORD=$(env_value POSTGRES_PASSWORD "$DATA_ENV")

# Old database settings. The MySQL root password goes to docker through the environment, not the command line.
if [ "$APP" = financas ]; then
  OLD_ENV=$A/.envs/.production/.postgres
  [ -f "$OLD_ENV" ] || die "$OLD_ENV not found"
  OLD_CT=ix-financas-postgres-1
  OLD_USER=$(env_value POSTGRES_USER "$OLD_ENV")
  OLD_DB=$(env_value POSTGRES_DB "$OLD_ENV")
else
  OLD_ENV=$A/.envs/.production/.mysql
  [ -f "$OLD_ENV" ] || die "$OLD_ENV not found"
  MYSQL_PWD=$(env_value MYSQL_ROOT_PASSWORD "$OLD_ENV")
  export MYSQL_PWD
  MYSQL_DATABASE=$(env_value MYSQL_DATABASE "$OLD_ENV")
  MYSQL_CT=ix-$APP-mysql-1
fi

# ---- helpers -------------------------------------------------------------------------------------

stop_app() {
  for s in api worker beat flower; do
    docker stop "ix-$APP-$s-1" >/dev/null 2>&1 || true
  done
}

# psql_new <database>: runs the SQL on stdin on the shared Postgres as the platform admin.
psql_new() { docker exec -i "$PG" psql -X -q -At -F '|' -v ON_ERROR_STOP=1 -U platform -d "$1"; }
psql_old() { docker exec -i "$OLD_CT" psql -X -q -At -F '|' -v ON_ERROR_STOP=1 -U "$OLD_USER" -d "$OLD_DB"; }
mysql_q() { docker exec -e MYSQL_PWD "$MYSQL_CT" mysql -uroot -N -B "$MYSQL_DATABASE" "$@"; }

new_tables() {
  echo "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1" | psql_new "$APP"
}

# A shell loop over table names is only safe for plain identifiers.
check_names() {
  for name in "$@"; do
    case "$name" in
      *[!A-Za-z0-9_]*) die "unexpected table name '$name'" ;;
    esac
  done
}

# Points MYSQL_CT at a temporary MySQL on the old data dir. pgloader cannot log in with MySQL 8's
# default caching_sha2_password, so this server runs with the native plugin and root is switched to it.
# The old container is only stopped, never removed: starting it again is the rollback.
ensure_mysql() {
  [ "$MYSQL_CT" = "$TEMP_MYSQL" ] && return 0
  if [ -n "$(docker ps -q -f "name=^$MYSQL_CT\$")" ]; then
    log "stopping $MYSQL_CT so a temporary MySQL can open its data dir"
    docker stop "$MYSQL_CT" >/dev/null || die "could not stop $MYSQL_CT"
  fi
  log "starting temporary MySQL $TEMP_MYSQL on $A/data/mysql"
  docker rm -f "$TEMP_MYSQL" >/dev/null 2>&1 || true
  docker run -d --name "$TEMP_MYSQL" --network platform-data --user 568:568 \
    -v "$A/data/mysql:/var/lib/mysql" mysql:8.0 --default-authentication-plugin=mysql_native_password >/dev/null
  MYSQL_CT=$TEMP_MYSQL
  trap remove_temp_mysql EXIT
  tries=0
  until docker exec -e MYSQL_PWD "$MYSQL_CT" mysqladmin -uroot ping --silent >/dev/null 2>&1; do
    tries=$((tries + 1))
    [ "$tries" -le 60 ] || die "temporary MySQL did not become ready"
    sleep 2
  done
  # The password goes through stdin, not the command line.
  quoted=$(printf '%s' "$MYSQL_PWD" | sed "s/'/''/g")
  printf "ALTER USER IF EXISTS 'root'@'%%' IDENTIFIED WITH mysql_native_password BY '%s';\nALTER USER IF EXISTS 'root'@'localhost' IDENTIFIED WITH mysql_native_password BY '%s';\n" "$quoted" "$quoted" \
    | docker exec -i -e MYSQL_PWD "$MYSQL_CT" mysql -uroot >/dev/null || die "could not switch root to mysql_native_password"
}

remove_temp_mysql() { docker rm -f "$TEMP_MYSQL" >/dev/null 2>&1 || true; }

mysql_pk_columns() {
  mysql_q -e "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = '$MYSQL_DATABASE' AND TABLE_NAME = '$1' AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION"
}

# stats_mysql <table> <pk columns>: prints "count|md5 of the ordered primary keys".
stats_mysql() {
  if [ -z "$2" ]; then
    mysql_q -e "SELECT COUNT(*), '' FROM $BT$1$BT" | tr '\t' '|'
    return
  fi
  list=""
  for c in $2; do list="$list${list:+, }$BT$c$BT"; done
  mysql_q -e "SET SESSION group_concat_max_len = 1073741824; SELECT COUNT(*), IFNULL(MD5(GROUP_CONCAT(k ORDER BY CAST(k AS BINARY) SEPARATOR ',')), '') FROM (SELECT CONCAT_WS('|', $list) AS k FROM $BT$1$BT) AS x" | tr '\t' '|'
}

stats_new_keys() {
  if [ -z "$2" ]; then
    echo "SELECT count(*), '' FROM \"$1\"" | psql_new "$APP"
    return
  fi
  list=""
  for c in $2; do list="$list${list:+, }\"$c\"::text"; done
  echo "SELECT count(*), coalesce(md5(string_agg(k, ',' ORDER BY k COLLATE \"C\")), '') FROM (SELECT concat_ws('|', $list) AS k FROM \"$1\") AS x" | psql_new "$APP"
}

# Whole-row content hash; COLLATE "C" keeps the row order identical in both databases,
# and UTC keeps timestamptz text identical when the two servers use different timezones.
row_stats_sql() {
  echo "SET TimeZone TO 'UTC'; SELECT count(*), coalesce(md5(string_agg(r::text, ',' ORDER BY r::text COLLATE \"C\")), '') FROM \"$1\" AS r"
}

# verify_all: prints a comparison table and returns 1 on any difference.
verify_all() {
  log "verifying $APP: old vs new"
  if [ "$APP" = financas ]; then
    old_list=$(echo "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'" | psql_old)
  else
    ensure_mysql
    old_list=$(mysql_q -e "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = '$MYSQL_DATABASE' AND TABLE_TYPE = 'BASE TABLE'")
  fi
  tables=$( { new_tables; echo "$old_list"; } | sed '/^$/d' | sort -u)
  [ -n "$tables" ] || die "no tables to verify"
  # shellcheck disable=SC2086
  check_names $tables
  bad=0
  printf '%-48s %10s %10s  %s\n' TABLE OLD NEW RESULT
  for t in $tables; do
    if [ "$APP" = financas ]; then
      old=$(row_stats_sql "$t" | psql_old 2>/dev/null) || old=ERROR
      new=$(row_stats_sql "$t" | psql_new "$APP" 2>/dev/null) || new=ERROR
    else
      # Django's own bookkeeping table is rebuilt by the new release's migrations.
      [ "$t" = django_migrations ] && continue
      cols=$(mysql_pk_columns "$t" 2>/dev/null) || cols=""
      old=$(stats_mysql "$t" "$cols" 2>/dev/null) || old=ERROR
      new=$(stats_new_keys "$t" "$cols" 2>/dev/null) || new=ERROR
    fi
    [ -n "$old" ] || old=ERROR
    [ -n "$new" ] || new=ERROR
    if [ "$old" = "$new" ] && [ "$old" != ERROR ]; then result=OK; else result=MISMATCH; bad=$((bad + 1)); fi
    printf '%-48s %10s %10s  %s\n' "$t" "${old%%|*}" "${new%%|*}" "$result"
  done
  if [ "$bad" -ne 0 ]; then
    warn "$bad table(s) differ; the app stays stopped"
    return 1
  fi
  log "all tables match"
}

# ---- steps ---------------------------------------------------------------------------------------

step_prepare() {
  marked done && die "migration of $APP already finished"
  if marked prepared; then log "prepare already done"; return 0; fi

  # 1. The target must be untouched, otherwise a restore or load would mix data.
  tables=$(new_tables)
  # shellcheck disable=SC2086
  check_names $tables
  if [ "$APP" = financas ]; then
    [ -z "$tables" ] || die "database financas on the shared Postgres already has tables; empty it first (see docs/shared-data-runbook.md)"
  else
    for t in $tables; do
      [ "$t" = django_migrations ] && continue
      [ "$(echo "SELECT count(*) FROM \"$t\"" | psql_new "$APP")" = 0 ] || die "table $t on the shared Postgres is not empty"
    done
  fi

  # 2. Infisical values for the new release.
  ask_required infisical_token "Infisical admin token (Infisical -> Organization -> Access Control -> Identities -> Instance Admin Identity -> Token Auth -> Create token, TTL 1h)" secret
  INFISICAL_TOKEN=$infisical_token
  export INFISICAL_TOKEN
  project_id=$(awk -v app="$APP" '$0 ~ "^  " app ":$" {on=1; next} on && /^  [^ ]/ {on=0} on && /project_id:/ {print $2; exit}' "$P/deploy/apps.yml" | tr -d "\"'")
  [ -n "$project_id" ] || die "no infisical project_id for $APP in $P/deploy/apps.yml"
  deployer_version=$(env_value DEPLOYER_VERSION "$P/deploy/.env")
  [ -n "$deployer_version" ] || die "no DEPLOYER_VERSION in $P/deploy/.env"
  redis_password=$(env_value REDIS_PASSWORD "$DATA_ENV")

  log "writing Infisical values for $APP"
  infisical_set postgres "POSTGRES_HOST=data-postgres" "POSTGRES_PORT=5432" "POSTGRES_DB=$APP" "POSTGRES_USER=$APP" "POSTGRES_PASSWORD=$NEW_PASSWORD"
  case "$APP" in
    games)
      infisical_set django "CELERY_BROKER_URL=redis://games:$redis_password@data-redis:6379/0"
      infisical_set flower "CELERY_BROKER_URL=redis://games:$redis_password@data-redis:6379/0"
      ;;
    financas)
      infisical_set django "REDIS_URL=redis://financas:$redis_password@data-redis:6379/1" \
        "CELERY_BROKER_URL=redis://financas:$redis_password@data-redis:6379/2" \
        "CELERY_RESULT_BACKEND=redis://financas:$redis_password@data-redis:6379/3"
      ;;
  esac

  # 3. Stop the writers, then take the dump of the old database: nothing changes after this point.
  log "stopping the $APP containers"
  stop_app
  mkdir -p "$A/backups"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  if [ "$APP" = financas ]; then
    dump="$A/backups/pre-migration-$stamp.dump"
    log "dumping the old database to $dump"
    (umask 077; docker exec "$OLD_CT" pg_dump -U "$OLD_USER" -Fc "$OLD_DB" > "$dump") || die "pg_dump failed"
  else
    [ -n "$(docker ps -q -f "name=^$MYSQL_CT\$")" ] || die "$MYSQL_CT is not running; the old database is needed for the dump"
    dump="$A/backups/pre-migration-$stamp.sql.gz"
    log "dumping the old database to $dump"
    (umask 077; docker exec -e MYSQL_PWD "$MYSQL_CT" mysqldump -uroot --single-transaction --routines --triggers --no-tablespaces "$MYSQL_DATABASE" > "$dump.tmp" && gzip -c "$dump.tmp" > "$dump") || die "mysqldump failed"
    rm -f "$dump.tmp"
  fi

  # 4. financas keeps its schema: restore the same dump into the shared database, then compare.
  if [ "$APP" = financas ]; then
    log "restoring the dump into the shared Postgres"
    echo "GRANT financas TO platform" | psql_new postgres
    # Ignored errors (for example the extension that provisioning already created) are judged by verify.
    docker exec -i "$PG" pg_restore -U platform -d financas --no-owner --role=financas < "$dump" \
      || warn "pg_restore reported errors; verify decides whether the data is complete"
    verify_all || die "restore does not match the old database"
  fi
  mark prepared
  notify "homelab: $APP migration prepared (dump $dump)"

  if [ "$APP" = financas ]; then
    log "next: merge the chore/shared-data PR; when the deploy finishes run: make nas-migrate APP=financas STEP=finish"
  else
    log "next: merge the chore/shared-postgres PR; when the deploy finishes run: make nas-migrate APP=$APP STEP=load"
  fi
}

# infisical_set <folder> KEY=VALUE...
infisical_set() {
  folder=$1
  shift
  docker run --rm --network platform_platform -e INFISICAL_TOKEN --entrypoint python \
    "ghcr.io/lucasehonda/homelab-deployer:$deployer_version" \
    -m deployer.tools.infisical_secrets --url http://infisical:8080 --project-id "$project_id" --folder "$folder" "$@" \
    || die "could not write the $folder secrets to Infisical"
}

step_load() {
  [ "$APP" != financas ] || die "financas has no load step; it restores in prepare"
  marked prepared || die "run STEP=prepare first"
  if marked loaded; then log "load already done"; return 0; fi
  [ -n "$(new_tables)" ] || die "the shared database has no schema yet; wait for the new release to deploy"

  # Remember what was running so a re-run after a failure still restarts the right containers.
  running=""
  for s in api worker beat flower; do
    if [ "$(docker inspect -f '{{.State.Running}}' "ix-$APP-$s-1" 2>/dev/null)" = true ]; then running="$running $s"; fi
  done
  if [ -n "$running" ]; then
    echo "$running" > "$M/stopped"
  elif [ -f "$M/stopped" ]; then
    running=$(cat "$M/stopped")
  fi
  log "stopping the $APP containers"
  stop_app

  ensure_mysql
  mysql_password=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$MYSQL_PWD")
  new_password=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$(env_value POSTGRES_PASSWORD "$P/.envs/.data-postgres")")
  load_file=$M/load.load
  (
    umask 077
    cat > "$load_file" <<LOAD
LOAD DATABASE
  FROM mysql://root:$mysql_password@$MYSQL_CT/$MYSQL_DATABASE
  INTO postgresql://platform:$new_password@data-postgres/$APP
WITH data only, truncate, disable triggers, reset sequences, prefetch rows = 1000
SET PostgreSQL PARAMETERS session_replication_role = 'replica'
CAST type tinyint when (= 1 precision) to boolean drop typemod
EXCLUDING TABLE NAMES MATCHING 'django_migrations'
ALTER SCHEMA '$MYSQL_DATABASE' RENAME TO 'public';
LOAD
  )
  log "loading the data with pgloader"
  loaded=0
  # v3.6.7 by digest; Docker Hub has no v3.6.9 tag.
  docker run --rm --network platform-data -v "$load_file:/load.load:ro" dimitri/pgloader@sha256:d29ea680cf1aaaf7269690a922dd69167567b91b35e9c48a0b54a99cef96c0ed pgloader /load.load && loaded=1
  rm -f "$load_file"
  [ "$loaded" -eq 1 ] || die "pgloader failed; the app stays stopped"

  verify_all || die "loaded data does not match; the app stays stopped"
  remove_temp_mysql
  for s in $running; do
    docker start "ix-$APP-$s-1" >/dev/null || warn "could not start ix-$APP-$s-1"
  done
  rm -f "$M/stopped"
  mark loaded
  notify "homelab: $APP data loaded into the shared Postgres"
  log "smoke test the app, then run: make nas-migrate APP=$APP STEP=finish"
}

step_verify() {
  marked prepared || die "run STEP=prepare first"
  verify_all
}

step_finish() {
  marked prepared || die "run STEP=prepare first"
  if marked done; then log "finish already done"; return 0; fi
  if [ "$APP" != financas ]; then marked loaded || die "run STEP=load first"; fi
  found=0
  for s in api worker beat flower; do
    ct=ix-$APP-$s-1
    state=$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$ct" 2>/dev/null) || continue
    found=1
    case "$state" in
      "running healthy" | "running ") log "$ct: ${state% }" ;;
      *) die "$ct is not healthy ($state)" ;;
    esac
  done
  [ "$found" -eq 1 ] || die "no $APP containers found; did the new release deploy?"
  mark done
  notify "homelab: $APP migrated to the shared data services"
  if [ "$APP" = financas ]; then old=postgres; else old=mysql; fi
  log "old database container data kept at $A/data/$old - delete it after 30 days"
}

case "$STEP" in
  prepare) step_prepare ;;
  load) step_load ;;
  verify) step_verify ;;
  finish) step_finish ;;
  *) echo "usage: nas-migrate-app-db.sh <games|pinguei|financas> <prepare|load|verify|finish>" >&2; exit 64 ;;
esac
