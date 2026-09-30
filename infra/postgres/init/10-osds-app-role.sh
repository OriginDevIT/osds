#!/bin/bash
# Runs once, on first cluster init (docker-entrypoint-initdb.d), as the owner.
# Creates the least-privilege role the app and worker connect as. It gets a
# login and nothing else: table privileges are granted after migrate by
# `manage.py grant_app_role`, and PG16 already denies CREATE on schema public.
#
# The password is the one generated on first boot into the osds-secrets volume.
# This script does not run against an existing data volume; a managed
# Postgres provisions this role under its own auth model.
set -euo pipefail

app_password="$(< /run/osds-secrets/db_app_password)"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v app_password="$app_password" <<'SQL'
create role osds_app login password :'app_password'
  nosuperuser nobypassrls nocreatedb nocreaterole;
SQL
