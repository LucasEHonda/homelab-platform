#!/bin/sh
# Emergency lockdown of the NAS. Run as root; the Makefile copies it over and runs it:
#
#   nas-lockdown.sh <platform dir> on|off|panic|unpanic
#
#   on       stops the deployer and the Tailscale nodes of the deployer and Infisical (`make lockdown`).
#   off      lifts `on` (`make unlock`). Refused while a panic is on.
#   panic    stops every running cloudflared container (the internet tunnels of every app), keeps them
#            stopped across reboots, then does `on` so no deploy can start them again (`make panic`).
#   unpanic  starts those tunnels again with their old restart policy, then does `off` (`make unpanic`).
#
# Apps keep running in every mode and stay reachable over Tailscale.
set -eu

P=${1:?usage: nas-lockdown.sh <platform dir> on|off|panic|unpanic}
ACTION=${2:?usage: nas-lockdown.sh <platform dir> on|off|panic|unpanic}
MARKER="$P/LOCKDOWN"
# One line per stopped tunnel: "<container name> <restart policy before the panic>".
PANIC="$P/PANIC"

[ "$(id -u)" -eq 0 ] || { echo "nas-lockdown: run as root (sudo $0 ...)" >&2; exit 77; }

compose() { docker compose -p platform -f "$P/deploy/compose.yml" --env-file "$P/deploy/.env" --project-directory "$P/deploy" "$@"; }

notify() {
  url=$(sed -n 's/^NTFY_URL=//p' "$P/.envs/.deployer" 2>/dev/null | tail -1)
  [ -n "$url" ] || return 0
  curl -fsS -m 10 -d "$1" "$url" >/dev/null 2>&1 || python3 -c 'import sys,urllib.request; urllib.request.urlopen(urllib.request.Request(sys.argv[1], data=sys.argv[2].encode()), timeout=10)' "$url" "$1" || true
}

lockdown_on() {
  date -u +%Y-%m-%dT%H:%M:%SZ > "$MARKER"
  compose stop deployer deployer-tailscale secrets-tailscale
  echo "==> lockdown on: deployer, deployer-tailscale and secrets-tailscale stopped; apps keep running"
}

lockdown_off() {
  rm -f "$MARKER"
  compose up -d deployer deployer-tailscale secrets-tailscale
  echo "==> lockdown off"
}

# docker ps lists only running containers, so a re-run keeps the lines of tunnels stopped earlier.
tunnels_off() {
  touch "$PANIC"
  chmod 600 "$PANIC"
  found=0
  for name in $(docker ps --format '{{.Names}}={{.Image}}'); do
    image=${name#*=}
    name=${name%%=*}
    case "$image" in
      cloudflare/cloudflared|cloudflare/cloudflared[:@]*|docker.io/cloudflare/cloudflared|docker.io/cloudflare/cloudflared[:@]*) ;;
      *) continue ;;
    esac
    found=1
    policy=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$name")
    grep -q "^$name " "$PANIC" || echo "$name ${policy:-no}" >> "$PANIC"
    # A stopped container with restart "always" comes back when Docker or the NAS restarts.
    docker update --restart=no "$name" >/dev/null
    docker stop "$name" >/dev/null
    echo "==> tunnel stopped: $name"
  done
  [ "$found" -eq 1 ] || echo "==> no cloudflared container running"
}

tunnels_on() {
  [ -f "$PANIC" ] || { echo "==> no panic on, no tunnel to start"; return 0; }
  while read -r name policy; do
    [ -n "$name" ] || continue
    if docker inspect "$name" >/dev/null 2>&1; then
      docker update --restart="$policy" "$name" >/dev/null
      docker start "$name" >/dev/null
      echo "==> tunnel started: $name"
    else
      echo "==> $name no longer exists; the next deploy of its app recreates it"
    fi
  done < "$PANIC"
  rm -f "$PANIC"
}

case "$ACTION" in
  on)
    lockdown_on
    notify "homelab: LOCKDOWN on - deployer and Infisical are off the tailnet"
    docker ps --filter name=platform- --format '{{.Names}} {{.Status}}'
    ;;
  off)
    if [ -f "$PANIC" ]; then
      echo "nas-lockdown: a panic is on ($PANIC); lift it with 'make unpanic', which also restarts the tunnels" >&2
      exit 1
    fi
    lockdown_off
    notify "homelab: lockdown off"
    ;;
  panic)
    tunnels_off
    lockdown_on
    notify "homelab: PANIC on - internet tunnels stopped, deploy path cut; apps only on Tailscale"
    docker ps --filter name=platform- --format '{{.Names}} {{.Status}}'
    ;;
  unpanic)
    tunnels_on
    lockdown_off
    notify "homelab: panic lifted - tunnels and deployer back"
    ;;
  *)
    echo "usage: nas-lockdown.sh <platform dir> on|off|panic|unpanic" >&2
    exit 64
    ;;
esac
