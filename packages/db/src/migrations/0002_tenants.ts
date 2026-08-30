/**
 * 0002_tenants - the tenant (directory) table, root of every FK chain.
 *
 * The one table with no `tenant_id` and no RLS: it *is* the tenant. Single- vs
 * multi-directory is the `mode` column - a UI toggle, never a data-model change
 * (spec §13, invariant 3).
 *
 * Rollback:
 *   drop table if exists tenants cascade;
 *   (cascades to every tenant-scoped table - roll those back first).
 *   Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types";
import { touchUpdatedAt } from "./helpers";

export async function up(db: MigrationDb): Promise<void> {
  await sql`
    create table tenants (
      id          text primary key check (starts_with(id, 'tnt_')),
      slug        text not null unique,
      domain      text unique,
      name        text not null,
      mode        text not null default 'single' check (mode in ('single', 'multi')),
      created_at  timestamptz not null default now(),
      updated_at  timestamptz not null default now()
    )
  `.execute(db);

  await touchUpdatedAt(db, "tenants");
}
