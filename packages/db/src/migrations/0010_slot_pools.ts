/**
 * 0010_slot_pools - capacity-limited premium placement pools (spec §6.6).
 *
 * One pool per (tier, scope), scope being global | category | location |
 * category_location. `capacity` and `locked` are the declared intent; `slots`
 * (0011) materialises one row per capacity unit. Sellable =
 * capacity - locked - occupied is derived from slot rows, not stored.
 *
 * Rollback:
 *   drop table if exists slot_pools cascade;
 *   (roll back 0011 first). Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types";
import { enableTenantRls, touchUpdatedAt } from "./helpers";

export async function up(db: MigrationDb): Promise<void> {
  await sql`
    create table slot_pools (
      id                 text primary key check (starts_with(id, 'pool_')),
      tenant_id          text not null references tenants (id) on delete cascade,
      tier               text not null,
      scope_type         text not null
                           check (scope_type in ('global', 'category', 'location',
                                                 'category_location')),
      scope_category     text,
      scope_locality     text,
      capacity           integer not null check (capacity >= 0),
      locked             integer not null default 0 check (locked >= 0),
      default_listing_id text,
      term_days          integer not null default 30 check (term_days in (30, 60, 90, 365)),
      created_at         timestamptz not null default now(),
      updated_at         timestamptz not null default now(),

      check (locked <= capacity),
      check (
        (scope_type = 'global' and scope_category is null and scope_locality is null)
        or (scope_type = 'category' and scope_category is not null and scope_locality is null)
        or (scope_type = 'location' and scope_category is null and scope_locality is not null)
        or (scope_type = 'category_location'
            and scope_category is not null and scope_locality is not null)
      ),

      unique (tenant_id, id),
      foreign key (tenant_id, tier) references tiers (tenant_id, key),
      foreign key (tenant_id, scope_category)
        references categories (tenant_id, id) on delete restrict,
      foreign key (tenant_id, default_listing_id)
        references listings (tenant_id, id) on delete set null
    )
  `.execute(db);

  await sql`
    create unique index slot_pools_scope on slot_pools (
      tenant_id, tier, scope_type,
      coalesce(scope_category, ''), coalesce(scope_locality, '')
    )
  `.execute(db);

  await enableTenantRls(db, "slot_pools");
  await touchUpdatedAt(db, "slot_pools");
}
