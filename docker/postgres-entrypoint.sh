#!/bin/bash
# Wraps the stock postgres entrypoint so the postgres container -- the first
# container up, and the only one that needs a secret before anything else --
# generates the install's secrets on first boot. No fifth container.
#
# Files land in the osds-secrets volume, written once and never rewritten:
#   db_owner_password   read by the stock entrypoint via POSTGRES_PASSWORD_FILE
#   db_app_password     read by infra/postgres/init/10-osds-app-role.sh
#   django_secret_key, osds_secret_key   read by the app entrypoint
#
# Values are hex, so they drop into a connection URL without escaping. Files
# are mode 0444: postgres (uid 999) and the app (uid 10001) share the volume
# and have no common group. The volume is mounted only into this stack's own
# containers.
#
# LOSING THE VOLUME LOSES THE KEYS. osds_secret_key encrypts the credentials
# the wizard stores; nothing else can decrypt them. See the README.
set -euo pipefail

dir=/run/osds-secrets
mkdir -p "$dir"

# generate NAME BYTES
generate() {
  local file="$dir/$1" tmp
  [ -s "$file" ] && return 0
  tmp="$(mktemp "$dir/.tmp.XXXXXX")"
  head -c "$2" /dev/urandom | od -An -tx1 | tr -d ' \n' >"$tmp"
  chmod 0444 "$tmp"
  mv "$tmp" "$file"
}

generate db_owner_password 24
generate db_app_password 24
generate django_secret_key 48
generate osds_secret_key 48

exec docker-entrypoint.sh "$@"
