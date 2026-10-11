#!/bin/sh
# Prints a Dozzle users.yml for one user on stdout. Runs on the operator's machine, so the password never
# reaches the NAS: only its bcrypt hash does. Used by `make nas-dozzle`.
#
#   dozzle-users.sh <user>
set -eu

DOZZLE_IMAGE=amir20/dozzle:v11.1.2
user=${1:?usage: dozzle-users.sh <user>}

printf 'Dozzle password for %s: ' "$user" >&2
stty -echo 2>/dev/null || true
read -r password
stty echo 2>/dev/null || true
printf '\n' >&2
[ -n "$password" ] || { echo "dozzle-users: empty password" >&2; exit 1; }

docker run --rm "$DOZZLE_IMAGE" generate "$user" --password "$password" --name "$user"
