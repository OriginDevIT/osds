/**
 * 0013_app_role - the least-privilege role the app and worker connect as.
 *
 * RLS is only enforced against roles that are neither the table owner nor hold
 * BYPASSRLS. `osds_app` is that role: DML on the tenant tables (plus read-only
 * `spatial_ref_sys` for PostGIS) and nothing more - no DDL, no SUPERUSER, no
 * BYPASSRLS. It is created NOLOGIN; granting it LOGIN and an auth method is a
 * deployment step (for local dev, docker-compose's init script does it).
 * Migrations keep running as the owner via DATABASE_URL_ADMIN.
 *
 * Rollback:
 *   alter default privileges in schema public revoke all on tables from osds_app;
 *   revoke all on all tables in schema public from osds_app;
 *   revoke all on schema public from osds_app;
 *   drop role if exists osds_app;
 *   Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types.js";

export async function up(db: MigrationDb): Promise<void> {
  await sql`
    do $$
    begin
      if not exists (select 1 from pg_roles where rolname = 'osds_app') then
        create role osds_app nologin nosuperuser nobypassrls;
      end if;
    end
    $$
  `.execute(db);

  // Never let this role bypass RLS or gain superuser, even if a prior
  // environment created it differently.
  await sql`alter role osds_app nosuperuser nobypassrls`.execute(db);

  await sql`grant usage on schema public to osds_app`.execute(db);

  await sql`
    grant select, insert, update, delete on
      tenants, tiers, categories, listing_categories, users, listings,
      claims, entitlements, slot_pools, slots, outbox
      to osds_app
  `.execute(db);

  // PostGIS spatial reference data - read-only, needed to evaluate distance and
  // bbox queries against the geography column.
  await sql`grant select on spatial_ref_sys to osds_app`.execute(db);

  // Cover tenant tables added by later migrations (created by the owner this
  // statement runs as).
  await sql`
    alter default privileges in schema public
      grant select, insert, update, delete on tables to osds_app
  `.execute(db);
}
