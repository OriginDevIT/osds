/**
 * 0011_slots - one row per unit of pool capacity; the row *is* the lock.
 *
 * Approved hold design: a hold is taken with a single
 *   UPDATE ... FROM (SELECT id FROM slots
 *                    WHERE pool_id = $p
 *                      AND (status = 'available'
 *                           OR (status = 'held' AND held_until < now()))
 *                    ORDER BY (status = 'available') DESC, slot_no
 *                    FOR UPDATE SKIP LOCKED LIMIT 1)
 * at READ COMMITTED. N racing callers each lock a distinct row or get zero
 * rows (-> "slot taken", immediately). Over-sell is impossible: the number of
 * slot rows in a pool never exceeds its capacity.
 *
 * status: available | locked | held | occupied | releasing. The *_shape CHECKs
 * keep the nullable columns consistent with status.
 * `slots_one_live_per_listing` (partial unique) is the multi-tab backstop.
 * `slot_no` is a stable per-pool ordinal for featured display order - it
 * survives releases, where occupied_at would reshuffle the page on renewal.
 * Application code mints `slot_` ULIDs and materialises / removes rows on pool
 * create and resize.
 *
 * Also adds the deferred FK entitlements.slot_id -> slots (tenant_id, id).
 *
 * Rollback:
 *   alter table entitlements drop constraint if exists entitlements_slot_id_fkey;
 *   drop table if exists slots;
 *   Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types.js";
import { enableTenantRls, touchUpdatedAt } from "./helpers.js";

export async function up(db: MigrationDb): Promise<void> {
  await sql`
    create table slots (
      id             text primary key check (starts_with(id, 'slot_')),
      tenant_id      text not null references tenants (id) on delete cascade,
      pool_id        text not null,
      slot_no        integer not null check (slot_no >= 1),
      status         text not null default 'available'
                       check (status in ('available', 'locked', 'held', 'occupied', 'releasing')),
      hold_kind      text check (hold_kind in ('checkout', 'trial')),
      listing_id     text,
      entitlement_id text,
      held_by        text,
      held_until     timestamptz,
      occupied_at    timestamptz,
      ends_at        timestamptz,
      created_at     timestamptz not null default now(),
      updated_at     timestamptz not null default now(),

      constraint slots_held_shape check (
        status <> 'held'
        or (listing_id is not null and held_until is not null and hold_kind is not null)
      ),
      constraint slots_occupied_shape check (
        status <> 'occupied'
        or (listing_id is not null and entitlement_id is not null and ends_at is not null)
      ),
      constraint slots_releasing_shape check (
        status <> 'releasing'
        or (listing_id is not null and ends_at is not null)
      ),
      constraint slots_free_shape check (
        status in ('held', 'occupied', 'releasing')
        or (listing_id is null and entitlement_id is null and held_by is null
            and held_until is null and hold_kind is null and occupied_at is null
            and ends_at is null)
      ),

      unique (tenant_id, id),
      unique (tenant_id, pool_id, slot_no),
      foreign key (tenant_id, pool_id)
        references slot_pools (tenant_id, id) on delete cascade,
      foreign key (tenant_id, listing_id)
        references listings (tenant_id, id) on delete restrict,
      foreign key (tenant_id, entitlement_id)
        references entitlements (tenant_id, id) on delete set null
    )
  `.execute(db);

  await sql`
    create unique index slots_one_live_per_listing
      on slots (tenant_id, pool_id, listing_id)
      where status in ('held', 'occupied', 'releasing')
  `.execute(db);
  await sql`
    create index slots_alloc on slots (tenant_id, pool_id, status)
      where status in ('available', 'held')
  `.execute(db);
  await sql`
    create index slots_hold_expiry on slots (held_until)
      where status = 'held'
  `.execute(db);
  await sql`
    create index slots_term_end on slots (ends_at)
      where status in ('occupied', 'releasing')
  `.execute(db);

  await enableTenantRls(db, "slots");
  await touchUpdatedAt(db, "slots");

  await sql`
    alter table entitlements
      add constraint entitlements_slot_id_fkey
      foreign key (tenant_id, slot_id) references slots (tenant_id, id)
      deferrable initially deferred
  `.execute(db);
}
