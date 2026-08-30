/**
 * 0008_claims - ownership claim lifecycle (spec §9).
 *
 * `consent` is NOT NULL with no default: core rejects `claim.submitted` without
 * it (§9.0, invariant 7). `verification` holds in-flight method state (method,
 * expires_at, attempt); `manual_verification` captures the §9.3 record when an
 * admin approves by hand.
 *
 * Rollback:
 *   drop table if exists claims;
 *   Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types.js";
import { enableTenantRls, touchUpdatedAt } from "./helpers.js";

export async function up(db: MigrationDb): Promise<void> {
  await sql`
    create table claims (
      id                  text primary key check (starts_with(id, 'claim_')),
      tenant_id           text not null references tenants (id) on delete cascade,
      listing_id          text not null,
      claimant_user_id    text,
      status              text not null default 'pending'
                            check (status in ('pending', 'verifying', 'approved',
                                              'rejected', 'abandoned', 'disputed')),
      method              text check (method in ('manual', 'phone_otp', 'domain_email',
                                                 'gbp_oauth', 'postcard')),
      consent             jsonb not null check (jsonb_typeof(consent) = 'object'),
      verification        jsonb not null default '{}',
      manual_verification jsonb,
      reason              text,
      decided_by          text,
      decided_at          timestamptz,
      created_at          timestamptz not null default now(),
      updated_at          timestamptz not null default now(),
      unique (tenant_id, id),
      foreign key (tenant_id, listing_id)
        references listings (tenant_id, id) on delete cascade,
      foreign key (tenant_id, claimant_user_id)
        references users (tenant_id, id) on delete set null,
      foreign key (tenant_id, decided_by)
        references users (tenant_id, id) on delete set null
    )
  `.execute(db);

  await sql`create index claims_by_listing on claims (tenant_id, listing_id)`.execute(db);
  await sql`create index claims_by_status on claims (tenant_id, status)`.execute(db);

  await enableTenantRls(db, "claims");
  await touchUpdatedAt(db, "claims");
}
