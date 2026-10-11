#!/bin/sh
# Installs or updates Dozzle (read-only container logs and stats) as a TrueNAS custom app, so it shows in the
# Apps tab. Run as root; `make nas-dozzle` copies it over with the users.yml made by dozzle-users.sh:
#
#   nas-dozzle.sh <users.yml>
#
# Env: PLATFORM_DIR (default /mnt/svd/platform), DOZZLE_PORT (default 8888).
set -eu

[ "$(id -u)" -eq 0 ] || { echo "nas-dozzle: run as root (sudo $0)" >&2; exit 77; }

USERS_FILE=${1:?usage: nas-dozzle.sh <users.yml>}
P=${PLATFORM_DIR:-/mnt/svd/platform}
PORT=${DOZZLE_PORT:-8888}
DATA=$P/data/dozzle
DOZZLE_IMAGE=amir20/dozzle:v11.1.2

log() { echo "==> $*"; }
die() { echo "nas-dozzle: $*" >&2; exit 1; }

[ -s "$USERS_FILE" ] || die "$USERS_FILE is empty"
grep -q '^users:' "$USERS_FILE" || die "$USERS_FILE is not a Dozzle users file"

log "writing $DATA/users.yml"
mkdir -p "$DATA"
install -m 600 "$USERS_FILE" "$DATA/users.yml"

# The docker socket is mounted read-only and DOZZLE_ENABLE_ACTIONS stays off: the page can show, never stop or remove.
compose=$(cat <<YAML
services:
  dozzle:
    image: $DOZZLE_IMAGE
    environment:
      DOZZLE_AUTH_PROVIDER: simple
      DOZZLE_NO_ANALYTICS: "true"
      DOZZLE_HOSTNAME: truenas
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - $DATA:/data
    ports:
      - "$PORT:8080"
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 128m
YAML
)

exists=$(midclt call app.query '[["name", "=", "dozzle"]]' | python3 -c 'import json, sys; print(len(json.load(sys.stdin)))')
if [ "$exists" = 0 ]; then
  if ss -ltn | awk '{print $4}' | grep -q ":$PORT\$"; then
    die "port $PORT is in use; run again with DOZZLE_PORT=<free port>"
  fi
  log "creating the TrueNAS app dozzle"
  payload=$(python3 -c 'import json, sys; print(json.dumps({"app_name": "dozzle", "custom_app": True, "custom_compose_config_string": sys.argv[1]}))' "$compose")
  midclt call -job app.create "$payload" >/dev/null
else
  log "updating the TrueNAS app dozzle"
  payload=$(python3 -c 'import json, sys; print(json.dumps({"custom_compose_config_string": sys.argv[1]}))' "$compose")
  midclt call -job app.update dozzle "$payload" >/dev/null
fi

host=$(hostname -I | awk '{print $1}')
log "Dozzle: http://$host:$PORT (Apps tab: dozzle)"
