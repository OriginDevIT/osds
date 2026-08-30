/**
 * 0001_extensions_and_functions - database prerequisites.
 *
 * Enables PostGIS (geography columns + GiST) and pg_trgm (fuzzy name search),
 * and creates two helpers used by later migrations and by RLS:
 *   - osds_current_tenant_id() : reads `app.tenant_id`, '' / unset -> NULL.
 *   - osds_set_updated_at()    : BEFORE UPDATE trigger, touches updated_at.
 *
 * Rollback:
 *   drop function if exists osds_set_updated_at();
 *   drop function if exists osds_current_tenant_id();
 *   drop extension if exists pg_trgm;
 *   drop extension if exists postgis;
 *   (only once every later migration has been rolled back). Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types.js";

export async function up(db: MigrationDb): Promise<void> {
  await sql`create extension if not exists postgis`.execute(db);
  await sql`create extension if not exists pg_trgm`.execute(db);

  await sql`
    create function osds_current_tenant_id() returns text
      language sql
      stable
      as $$ select nullif(current_setting('app.tenant_id', true), '') $$
  `.execute(db);

  await sql`
    create function osds_set_updated_at() returns trigger
      language plpgsql
      as $$
      begin
        new.updated_at := now();
        return new;
      end
      $$
  `.execute(db);
}
