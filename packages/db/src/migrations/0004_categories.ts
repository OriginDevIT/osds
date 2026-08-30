/**
 * 0004_categories - listing taxonomy, tenant-scoped, optionally hierarchical.
 *
 * `cat_`-prefixed ULID PK. The spec names category *slugs* but no PK scheme; a
 * stable surrogate keeps the join and the slot-pool scope FK simple.
 * `unique (tenant_id, id)` backs those composite FKs.
 *
 * Rollback:
 *   drop table if exists categories cascade;
 *   (roll back 0007 and 0010 first). Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types.js";
import { enableTenantRls, touchUpdatedAt } from "./helpers.js";

export async function up(db: MigrationDb): Promise<void> {
  await sql`
    create table categories (
      id          text primary key check (starts_with(id, 'cat_')),
      tenant_id   text not null references tenants (id) on delete cascade,
      slug        text not null,
      name        text not null,
      parent_id   text,
      created_at  timestamptz not null default now(),
      updated_at  timestamptz not null default now(),
      unique (tenant_id, id),
      unique (tenant_id, slug),
      foreign key (tenant_id, parent_id)
        references categories (tenant_id, id) on delete set null
    )
  `.execute(db);

  await enableTenantRls(db, "categories");
  await touchUpdatedAt(db, "categories");
}
