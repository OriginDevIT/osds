/**
 * 0007_listing_categories - listing <-> category join.
 *
 * Composite FKs carry `tenant_id` into both parents, so a row can never pair a
 * listing and a category from different tenants. Rows are immutable (no
 * updated_at, no touch trigger).
 *
 * Rollback:
 *   drop table if exists listing_categories;
 *   (do this before dropping listings or categories). Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types.js";
import { enableTenantRls } from "./helpers.js";

export async function up(db: MigrationDb): Promise<void> {
  await sql`
    create table listing_categories (
      tenant_id    text not null references tenants (id) on delete cascade,
      listing_id   text not null,
      category_id  text not null,
      created_at   timestamptz not null default now(),
      primary key (tenant_id, listing_id, category_id),
      foreign key (tenant_id, listing_id)
        references listings (tenant_id, id) on delete cascade,
      foreign key (tenant_id, category_id)
        references categories (tenant_id, id) on delete cascade
    )
  `.execute(db);

  await sql`
    create index listing_categories_by_category
      on listing_categories (tenant_id, category_id)
  `.execute(db);

  await enableTenantRls(db, "listing_categories");
}
