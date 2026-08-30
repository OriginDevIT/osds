/**
 * 0003_tiers - per-tenant ordered tier list (spec §4.2).
 *
 * Core hardcodes no tier names. `rank` 0 is the fallback tier; a tenant may
 * define none, which changes downgrade behaviour (§6.4). PK (tenant_id, key)
 * is the target of every `(tenant_id, tier)` FK elsewhere.
 *
 * Rollback:
 *   drop table if exists tiers;
 *   (blocked while listings / entitlements / slot_pools reference it - roll
 *   those back first). Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types";
import { enableTenantRls, touchUpdatedAt } from "./helpers";

export async function up(db: MigrationDb): Promise<void> {
  await sql`
    create table tiers (
      tenant_id    text not null references tenants (id) on delete cascade,
      key          text not null,
      rank         integer not null check (rank >= 0),
      purchasable  boolean not null default false,
      uses_slot    boolean not null default false,
      created_at   timestamptz not null default now(),
      updated_at   timestamptz not null default now(),
      primary key (tenant_id, key),
      unique (tenant_id, rank)
    )
  `.execute(db);

  await enableTenantRls(db, "tiers");
  await touchUpdatedAt(db, "tiers");
}
