#!/bin/sh
# Installs or updates the platform (Infisical, deployer, Tailscale sidecars) on TrueNAS SCALE.
# Run as root; `make nas-setup` copies the files over and runs it:
#
#   nas-setup.sh <source dir> <platform dir> <app spec>...
#
# app spec = name,repository,app_dir,image_prefix,repository_id
# Env: DEPLOYER_VERSION (required), GENERATE (optional, space-separated --generate values).
# Every step is idempotent.
set -eu

SRC=${1:?usage: nas-setup.sh <source dir> <platform dir> <app spec>...}
P=${2:?usage: nas-setup.sh <source dir> <platform dir> <app spec>...}
shift 2
DEPLOYER_VERSION=${DEPLOYER_VERSION:?DEPLOYER_VERSION is required}
GENERATE=${GENERATE:-}

[ "$(id -u)" -eq 0 ] || { echo "nas-setup: run as root (sudo $0 ...)" >&2; exit 77; }
SRC=$(cd "$SRC" && pwd)

log() { echo "==> $*"; }
warn() { echo "nas-setup: $*" >&2; }

# A lockdown must be lifted on purpose (make unlock), never by a routine setup run.
if [ -f "$P/LOCKDOWN" ]; then
  warn "the platform is in lockdown since $(cat "$P/LOCKDOWN"); run make unlock first"
  exit 1
fi
has_midclt() { command -v midclt >/dev/null 2>&1; }
random_secret() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
env_value() { sed -n "s/^$1=//p" "$2" | tail -1; }
compose() { docker compose -p platform -f "$P/deploy/compose.yml" --env-file "$P/deploy/.env" --project-directory "$P/deploy" "$@"; }

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

create_dataset() {
  [ -d "$P" ] && return 0
  dataset=${P#/mnt/}
  if has_midclt && midclt call pool.dataset.create "{\"name\": \"$dataset\"}" >/dev/null; then
    log "created dataset $dataset"
  else
    warn "could not create dataset $dataset; using a plain folder"
    mkdir -p "$P"
  fi
}

# write_file <path>: writes stdin to <path> with mode 0600 from the first byte.
write_file() {
  python3 -c '
import os, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as out:
    out.write(sys.stdin.read())
' "$1"
}

app_name() { echo "${1%%,*}"; }

# 1. Folders
log "folders in $P"
create_dataset
mkdir -p "$P/deploy" "$P/.envs" "$P/data/deployer" "$P/data/infisical-db" \
  "$P/data/tailscale-deployer" "$P/data/tailscale-secrets"
chown 568:568 "$P/data/infisical-db"
chmod 700 "$P/.envs"

# 2. Deploy files (apps.yml and .env are owned by the server)
log "deploy files"
(cd "$SRC/deploy" && tar cf - --exclude=apps.yml --exclude=.env .) | (cd "$P/deploy" && tar xf -)

# 3. Questions
envs="$P/.envs"
tailnet=""
admin_email=""
if [ ! -f "$envs/.infisical" ] || [ ! -f "$P/deploy/apps.yml" ]; then
  ask_required tailnet "Tailnet DNS name (Tailscale admin -> DNS, e.g. tail1234.ts.net)"
fi
if [ ! -f "$P/deploy/apps.yml" ]; then
  ask_required admin_email "Your email (Infisical admin login and break-glass admin)"
fi
if [ ! -f "$envs/.tailscale-deployer" ]; then
  ask_required ts_deployer "Tailscale auth key (tskey-auth-..., tag:deployer)" secret
fi
if [ ! -f "$envs/.tailscale-secrets" ]; then
  ask_required ts_secrets "Tailscale auth key (tskey-auth-..., tag:secrets)" secret
fi
if [ ! -f "$envs/docker-config.json" ]; then
  log "ghcr.io login for the deployer (GitHub classic token with only read:packages)"
  ask_required ghcr_user "GitHub user"
  ask_required ghcr_token "Token (ghp_...)" secret
fi

# 4. Secret files
if [ ! -f "$envs/.infisical" ]; then
  if [ -f "$envs/.infisical-db" ]; then
    db_password=$(env_value POSTGRES_PASSWORD "$envs/.infisical-db")
  else
    log "writing .envs/.infisical-db"
    db_password=$(random_secret 32)
    printf 'POSTGRES_USER=infisical\nPOSTGRES_PASSWORD=%s\nPOSTGRES_DB=infisical\n' "$db_password" \
      | write_file "$envs/.infisical-db"
  fi
  log "writing .envs/.infisical"
  encryption_key=$(python3 -c "import secrets; print(secrets.token_hex(16))")
  auth_secret=$(python3 -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())")
  printf 'ENCRYPTION_KEY=%s\nAUTH_SECRET=%s\nDB_CONNECTION_URI=postgres://infisical:%s@infisical-db:5432/infisical\nREDIS_URL=redis://infisical-redis:6379\nSITE_URL=https://secrets.%s\nTELEMETRY_ENABLED=false\n' \
    "$encryption_key" "$auth_secret" "$db_password" "$tailnet" | write_file "$envs/.infisical"
fi
if [ ! -f "$envs/.tailscale-deployer" ]; then
  log "writing .envs/.tailscale-deployer"
  printf 'TS_AUTHKEY=%s\nTS_EXTRA_ARGS=--advertise-tags=tag:deployer\n' "$ts_deployer" \
    | write_file "$envs/.tailscale-deployer"
fi
if [ ! -f "$envs/.tailscale-secrets" ]; then
  log "writing .envs/.tailscale-secrets"
  printf 'TS_AUTHKEY=%s\nTS_EXTRA_ARGS=--advertise-tags=tag:secrets\n' "$ts_secrets" \
    | write_file "$envs/.tailscale-secrets"
fi
if [ ! -f "$envs/.deployer" ]; then
  log "writing .envs/.deployer"
  python3 -c "import secrets; print('NTFY_URL=https://ntfy.sh/homelab-' + secrets.token_hex(16))" \
    | write_file "$envs/.deployer"
fi
if [ ! -f "$envs/docker-config.json" ]; then
  log "writing .envs/docker-config.json"
  GHCR_USER=$ghcr_user GHCR_TOKEN=$ghcr_token python3 -c '
import base64, json, os
auth = base64.b64encode("{}:{}".format(os.environ["GHCR_USER"], os.environ["GHCR_TOKEN"]).encode()).decode()
print(json.dumps({"auths": {"ghcr.io": {"auth": auth}}}))
' | write_file "$envs/docker-config.json"
fi

# 5. deploy/.env
if [ ! -f "$P/deploy/.env" ]; then
  cp "$P/deploy/.env.example" "$P/deploy/.env"
fi
if grep -q '^DEPLOYER_VERSION=' "$P/deploy/.env"; then
  sed -i "s|^DEPLOYER_VERSION=.*|DEPLOYER_VERSION=$DEPLOYER_VERSION|" "$P/deploy/.env"
else
  printf 'DEPLOYER_VERSION=%s\n' "$DEPLOYER_VERSION" >> "$P/deploy/.env"
fi

# 6. deploy/apps.yml
if [ ! -f "$P/deploy/apps.yml" ]; then
  log "writing deploy/apps.yml"
  python3 - "$admin_email" "$@" <<'PY' | write_file "$P/deploy/apps.yml"
import sys

admin, *specs = sys.argv[1:]
lines = [
    "infisical_url: http://infisical:8080",
    "ntfy_url_env: NTFY_URL",
    "allowed_workflow_refs:",
    "  - LucasEHonda/homelab-platform/.github/workflows/deploy.yml@",
    "admins:",
    "  - " + admin,
    "apps:",
]
for spec in specs:
    name, repository, app_dir, image_prefix, repository_id = spec.split(",")
    prefix = name.upper().replace("-", "_")
    lines += [
        "  %s:" % name,
        "    repository: %s" % repository,
        '    repository_id: "%s"' % repository_id,
        "    app_dir: %s" % app_dir,
        "    image_prefixes:",
        "      - %s" % image_prefix,
        "    infisical:",
        "      project_id: replace-with-project-id",
        "      environment: prod",
        "      client_id_env: %s_INFISICAL_CLIENT_ID" % prefix,
        "      client_secret_env: %s_INFISICAL_CLIENT_SECRET" % prefix,
    ]
print("\n".join(lines))
PY
fi

# 7. Infisical and its Tailscale sidecar
log "starting Infisical"
compose pull --quiet
compose up -d infisical-db infisical-redis infisical secrets-tailscale

# 8. Bootstrap: Infisical admin, projects, app secrets, deployer credentials
if grep -q replace-with-project-id "$P/deploy/apps.yml"; then
  ask_required password "Infisical admin password (new account)" secret
  admin_email=$(python3 -c '
import re, sys
match = re.search(r"^admins:\s*\n\s*-\s*(\S+)", open(sys.argv[1]).read(), re.M)
print(match.group(1) if match else "")
' "$P/deploy/apps.yml")
  [ -n "$admin_email" ] || { warn "no admin email in deploy/apps.yml"; exit 1; }
  app_mounts=""
  for spec in "$@"; do
    app_dir=$(echo "$spec" | cut -d, -f3)
    app_mounts="$app_mounts -v $app_dir:$app_dir:ro"
  done
  generate_args=""
  for value in $GENERATE; do
    generate_args="$generate_args --generate $value"
  done
  log "bootstrapping Infisical"
  # shellcheck disable=SC2086
  INFISICAL_ADMIN_PASSWORD="$password" docker run --rm --network platform_platform \
    -e INFISICAL_ADMIN_PASSWORD \
    -v "$P/deploy/apps.yml:/config/apps.yml" -v "$P/.envs:/envs" $app_mounts \
    --entrypoint python "ghcr.io/lucasehonda/homelab-deployer:$DEPLOYER_VERSION" \
    -m deployer.tools.infisical_bootstrap --url http://infisical:8080 \
    --apps-file /config/apps.yml --deployer-env /envs/.deployer --admin-email "$admin_email" $generate_args \
    || { warn "bootstrap failed; fix the error above and run again"; exit 1; }
fi

# 9. Old polling cron jobs: the deployer replaces them
for spec in "$@"; do
  name=$(app_name "$spec")
  has_midclt || continue
  ids=$(midclt call cronjob.query "[[\"description\", \"=\", \"$name auto-update\"]]" | python3 -c 'import json,sys; print(" ".join(str(job["id"]) for job in json.load(sys.stdin)))')
  for id in $ids; do
    midclt call cronjob.delete "$id" >/dev/null
    log "removed the old $name auto-update cron job $id"
  done
done

# 10. Whole stack
log "starting the whole stack"
compose up -d --remove-orphans

# 11. Summary
site_url=$(env_value SITE_URL "$envs/.infisical")
tailnet=${site_url#https://secrets.}
log "ntfy: subscribe to $(env_value NTFY_URL "$envs/.deployer") in the ntfy app"
log "Infisical: https://secrets.$tailnet"
log "Deployer: https://deployer.$tailnet"
log "Keep an offline copy of ENCRYPTION_KEY: sudo grep ENCRYPTION_KEY $P/.envs/.infisical"
