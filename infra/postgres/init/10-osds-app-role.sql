-- Local dev only. Runs once, on first cluster init
-- (docker-entrypoint-initdb.d). Gives the least-privilege application role a
-- login for the dev container; its table privileges are granted by migration
-- 0013. Production provisions this role under its own auth model.
--
-- If your pgdata volume already exists, this file will not have run - create
-- the login by hand once:
--   docker compose exec postgres psql -U osds -c \
--     "create role osds_app login password 'osds_dev_only' nosuperuser nobypassrls;"
-- or reset the volume with:  pnpm infra:reset

create role osds_app login password 'osds_dev_only' nosuperuser nobypassrls;
