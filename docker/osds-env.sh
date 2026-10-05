# Sourced by entrypoint.sh and osds-manage; not executable on its own.
#
# The one place that turns the osds-secrets volume into the environment
# settings.py expects. `docker compose exec` does not inherit what the
# entrypoint exported, so anything run that way must source this too.
#
# A variable that is already set is never overwritten, so an operator's .env
# override always wins; when neither exists the variable stays unset and
# settings.py raises at import, as it always has.

secrets_dir="${OSDS_SECRETS_DIR:-/run/osds-secrets}"
db_location="${OSDS_DB_HOST:-postgres}:${OSDS_DB_PORT:-5432}/${OSDS_DB_NAME:-osds}"

secret_file() { printf '%s/%s' "$secrets_dir" "$1"; }

# export_from_secret VAR FILE -- set VAR from the secrets volume unless set.
export_from_secret() {
  local var="$1" file
  file="$(secret_file "$2")"
  if [ -z "${!var:-}" ] && [ -r "$file" ]; then
    export "$var=$(<"$file")"
  fi
}

# db_url_from_secret ROLE FILE -- print the URL for ROLE, or nothing if the
# password file is not readable.
db_url_from_secret() {
  local file
  file="$(secret_file "$2")"
  if [ -r "$file" ]; then
    printf 'postgresql://%s:%s@%s' "$1" "$(<"$file")" "$db_location"
  fi
}

# load_app_env -- the runtime environment for the app role. Never touches the
# owner credential.
load_app_env() {
  export_from_secret DJANGO_SECRET_KEY django_secret_key
  export_from_secret OSDS_SECRET_KEY osds_secret_key
  if [ -z "${DATABASE_URL:-}" ]; then
    local url
    url="$(db_url_from_secret osds_app db_app_password)"
    if [ -n "$url" ]; then export DATABASE_URL="$url"; fi
  fi
}

# require_app_env -- exit 1 naming the first of the three that is still unset.
require_app_env() {
  local var
  for var in DJANGO_SECRET_KEY OSDS_SECRET_KEY DATABASE_URL; do
    if [ -z "${!var:-}" ]; then
      echo "osds: $var is not set and no generated value was found in $secrets_dir." >&2
      echo "osds: the osds-secrets volume is written by the postgres container on first boot;" >&2
      echo "osds: if it was deleted, see the README before starting over." >&2
      exit 1
    fi
  done
}
