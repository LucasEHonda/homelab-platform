#!/bin/sh
# Sets what the shared deploy workflow needs in each app repository:
#   variable DEPLOYER_URL, and either the Tailscale OAuth client (secrets TS_OAUTH_CLIENT_ID and
#   TS_OAUTH_SECRET) or a workload identity federation client (secret TS_OAUTH_CLIENT_ID and
#   variable TS_AUDIENCE).
# Usage: scripts/github-setup.sh owner/repo [owner/repo...]
set -eu

[ "$#" -gt 0 ] || { echo "usage: $0 owner/repo [owner/repo...]" >&2; exit 64; }
command -v gh >/dev/null || { echo "github-setup: gh is not installed" >&2; exit 69; }

printf 'Tailnet DNS name (e.g. tail1234.ts.net): '
IFS= read -r tailnet
printf 'Tailscale client ID: '
IFS= read -r client_id
printf 'Tailscale OAuth client secret (empty when using workload identity federation): '
stty -echo
IFS= read -r client_secret
stty echo
echo
audience=""
if [ -z "$client_secret" ]; then
  printf 'Workload identity federation audience: '
  IFS= read -r audience
fi
[ -n "$tailnet" ] && [ -n "$client_id" ] || { echo "github-setup: tailnet and client ID are required" >&2; exit 64; }
[ -n "$client_secret" ] || [ -n "$audience" ] || { echo "github-setup: give a client secret or an audience" >&2; exit 64; }

for repo in "$@"; do
  echo "==> $repo"
  gh variable set DEPLOYER_URL --repo "$repo" --body "https://deployer.$tailnet"
  printf '%s' "$client_id" | gh secret set TS_OAUTH_CLIENT_ID --repo "$repo"
  if [ -n "$client_secret" ]; then
    printf '%s' "$client_secret" | gh secret set TS_OAUTH_SECRET --repo "$repo"
    gh variable delete TS_AUDIENCE --repo "$repo" >/dev/null 2>&1 || true
  else
    gh variable set TS_AUDIENCE --repo "$repo" --body "$audience"
    gh secret delete TS_OAUTH_SECRET --repo "$repo" >/dev/null 2>&1 || true
  fi
done
echo "done"
