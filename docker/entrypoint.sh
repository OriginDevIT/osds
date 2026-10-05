#!/bin/bash
# Container entrypoint for osds-app and osds-worker: `entrypoint.sh app|worker`.
#
# Secrets. First boot, the postgres container generates the install's secrets
# into the osds-secrets volume (docker/postgres-entrypoint.sh). This script
# reads them from there -- but only for a variable that is not already set, so
# an operator's .env override always wins and, when neither exists, the
# variable stays unset and settings.py raises at import, as it always has.
#
# Migrations run as the database owner (decisions.md section 4). The owner URL
# lives in a shell variable that is never exported to the app or the worker.
set -euo pipefail

role="${1:-app}"

# Shared with osds-manage so `exec` and boot build the environment one way.
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/osds-env.sh"

load_app_env
admin_url="${DATABASE_URL_ADMIN:-}"
if [ -z "$admin_url" ]; then
  admin_url="$(db_url_from_secret osds db_owner_password)"
fi
# Not exported: the running app and worker never see the owner credential.
unset DATABASE_URL_ADMIN

require_app_env

case "$role" in
  worker)
    # osds-app owns migration; the compose file starts the worker only after
    # osds-app reports healthy, which is after migrate has finished.
    exec python manage.py run_worker
    ;;
  app)
    if [ -z "$admin_url" ]; then
      echo "osds: no DATABASE_URL_ADMIN and no generated owner password; cannot migrate." >&2
      exit 1
    fi

    # Wait for the server itself. Compose gates on postgres being healthy, but
    # the same image runs against a managed database with no such gate.
    for attempt in $(seq 1 30); do
      if ADMIN_URL="$admin_url" python - <<'PY'
import os, sys, psycopg
try:
    psycopg.connect(os.environ["ADMIN_URL"], connect_timeout=3).close()
except psycopg.Error:
    sys.exit(1)
PY
      then break; fi
      if [ "$attempt" = 30 ]; then echo "osds: database unreachable after 30 attempts." >&2; exit 1; fi
      sleep 2
    done

    DATABASE_URL="$admin_url" python manage.py migrate --noinput

    # Name of the role the app connects as, parsed from its own URL.
    app_role="${DATABASE_URL#*://}"
    app_role="${app_role%%[:@]*}"
    DATABASE_URL="$admin_url" python manage.py grant_app_role --role "$app_role"

    # Prints the first-run token to stdout, i.e. to `docker compose logs`.
    python manage.py ensure_setup_token

    exec gunicorn osds.wsgi:application \
      --bind 0.0.0.0:8000 \
      --workers "${GUNICORN_WORKERS:-3}" \
      --logger-class osds.gunicorn_logging.MaskingLogger \
      --access-logfile - --error-logfile -
    ;;
  *)
    echo "usage: entrypoint.sh app|worker" >&2
    exit 2
    ;;
esac
