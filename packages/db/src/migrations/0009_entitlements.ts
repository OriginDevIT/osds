/**
 * 0009_entitlements - tier/period state, owned by core (spec §6.1-§6.3).
 *
 * One live row per listing (partial unique excludes expired / canceled).
 * Partial indexes on trial_ends_at / current_period_end / grace_ends_at feed
 * the worker's dunning, grace-expiry and term-expiry jobs (§13). `slot_id`
 * gets its FK in 0011 once `slots` exists - DEFERRABLE, because an entitlement
 * and its slot are written in one transaction.
 *
 * Rollback:
 *   drop table if exists entitlements;
 *   (drop 0011's entitlements_slot_id_fkey first). Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types";
import { enableTenantRls, touchUpdatedAt } from "./helpers";

export async function up(db: MigrationDb): Promise<void> {
  await sql`
    create table entitlements (
      id                   text primary key check (starts_with(id, 'ent_')),
      tenant_id            text not null references tenants (id) on delete cascade,
      listing_id           text not null,
      tier                 text not null,
      status               text not null
                             check (status in ('none', 'trialing', 'active', 'past_due',
                                               'grace', 'expired', 'canceled', 'comped')),
      billing_mode         text not null
                             check (billing_mode in ('recurring', 'term', 'comp', 'none')),
      term_days            integer check (term_days is null or term_days in (30, 60, 90, 365)),
      started_at           timestamptz,
      current_period_end   timestamptz,
      trial_ends_at        timestamptz,
      dunning_started_at   timestamptz,
      grace_ends_at        timestamptz,
      slot_id              text,
      cancel_at_period_end boolean not null default false,
      comp                 jsonb,
      payment_ref          jsonb,
      created_at           timestamptz not null default now(),
      updated_at           timestamptz not null default now(),
      unique (tenant_id, id),
      foreign key (tenant_id, listing_id)
        references listings (tenant_id, id) on delete cascade,
      foreign key (tenant_id, tier) references tiers (tenant_id, key)
    )
  `.execute(db);

  await sql`
    create unique index entitlements_one_live_per_listing
      on entitlements (tenant_id, listing_id)
      where status not in ('expired', 'canceled')
  `.execute(db);
  await sql`
    create index entitlements_trial_due on entitlements (trial_ends_at)
      where status = 'trialing'
  `.execute(db);
  await sql`
    create index entitlements_period_end on entitlements (current_period_end)
      where status in ('active', 'past_due')
  `.execute(db);
  await sql`
    create index entitlements_grace_end on entitlements (grace_ends_at)
      where status = 'grace'
  `.execute(db);
  await sql`create index entitlements_by_status on entitlements (tenant_id, status)`.execute(db);

  await enableTenantRls(db, "entitlements");
  await touchUpdatedAt(db, "entitlements");
}
