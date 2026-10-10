#!/bin/sh
# Read-only resource snapshot of the app stacks on the NAS. Run as root; `make nas-stats` copies it over and runs it:
#
#   nas-stats.sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "nas-stats: run as root (sudo $0)" >&2; exit 77; }

PROJECTS="ix-games ix-pinguei ix-financas platform"

container() { docker ps -q --filter "label=com.docker.compose.project=$1" --filter "label=com.docker.compose.service=$2" | head -1; }

echo "==> host memory"
free -m

for project in $PROJECTS; do
  echo
  echo "==> $project containers"
  ids=$(docker ps -q --filter "label=com.docker.compose.project=$project")
  if [ -z "$ids" ]; then
    echo "(no running containers)"
    continue
  fi
  # shellcheck disable=SC2086
  docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}' $ids
done

for spec in ix-games:redis ix-financas:redis platform:infisical-redis; do
  project=${spec%%:*}
  service=${spec#*:}
  id=$(container "$project" "$service")
  [ -n "$id" ] || continue
  echo
  echo "==> $project/$service redis memory and keys"
  docker exec "$id" redis-cli INFO memory | grep -E '^(used_memory_human|used_memory_peak_human|maxmemory_human|maxmemory_policy):'
  docker exec "$id" redis-cli INFO keyspace | grep '^db' || echo "(no keys)"
done

for project in ix-games ix-pinguei; do
  id=$(container "$project" mysql)
  [ -n "$id" ] || continue
  echo
  echo "==> $project/mysql data size per database (MB)"
  docker exec "$id" sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -e "SELECT table_schema, ROUND(SUM(data_length + index_length) / 1048576, 1) FROM information_schema.tables GROUP BY table_schema;"' 2>/dev/null || echo "(could not query)"
done

for spec in ix-financas:postgres platform:infisical-db; do
  project=${spec%%:*}
  service=${spec#*:}
  id=$(container "$project" "$service")
  [ -n "$id" ] || continue
  echo
  echo "==> $project/$service database sizes"
  docker exec "$id" sh -c 'psql -U "$POSTGRES_USER" -d postgres -At -c "SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database WHERE NOT datistemplate;"' || echo "(could not query)"
done
