#!/bin/sh
# Emergency lockdown of the deploy path on the NAS. Run as root; `make lockdown` / `make unlock` copy it over and run it:
#
#   nas-lockdown.sh <platform dir> on|off
set -eu

P=${1:?usage: nas-lockdown.sh <platform dir> on|off}
ACTION=${2:?usage: nas-lockdown.sh <platform dir> on|off}
MARKER="$P/LOCKDOWN"

[ "$(id -u)" -eq 0 ] || { echo "nas-lockdown: run as root (sudo $0 ...)" >&2; exit 77; }

compose() { docker compose -p platform -f "$P/deploy/compose.yml" --env-file "$P/deploy/.env" --project-directory "$P/deploy" "$@"; }

notify() {
  url=$(sed -n 's/^NTFY_URL=//p' "$P/.envs/.deployer" 2>/dev/null | tail -1)
  [ -n "$url" ] || return 0
  curl -fsS -m 10 -d "$1" "$url" >/dev/null 2>&1 || python3 -c 'import sys,urllib.request; urllib.request.urlopen(urllib.request.Request(sys.argv[1], data=sys.argv[2].encode()), timeout=10)' "$url" "$1" || true
}

case "$ACTION" in
  on)
    date -u +%Y-%m-%dT%H:%M:%SZ > "$MARKER"
    compose stop deployer deployer-tailscale secrets-tailscale
    notify "homelab: LOCKDOWN on - deployer and Infisical are off the tailnet"
    echo "==> lockdown on: deployer, deployer-tailscale and secrets-tailscale stopped; apps keep running"
    docker ps --filter name=platform- --format '{{.Names}} {{.Status}}'
    ;;
  off)
    rm -f "$MARKER"
    compose up -d deployer deployer-tailscale secrets-tailscale
    notify "homelab: lockdown off"
    echo "==> lockdown off"
    ;;
  *)
    echo "usage: nas-lockdown.sh <platform dir> on|off" >&2
    exit 64
    ;;
esac
