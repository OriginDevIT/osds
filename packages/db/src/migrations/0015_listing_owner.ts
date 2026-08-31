/**
 * 0015_listing_owner - the current-owner column on `listings` (issue #45).
 *
 * `claim.approve` assigns ownership: from this migration on it sets
 * `owner_user_id` alongside `status = 'claimed'`, to the approved claim's
 * claimant. Nullable - a listing with no approved claim has no owner. The
 * composite FK carries `tenant_id` into `users`, so a listing can never be
 * owned by a user from another tenant. `on delete restrict`: a user who owns a
 * listing cannot be deleted until ownership is reassigned or cleared.
 *
 * This column is the current-owner projection; `listing.owner_assigned` remains
 * the assignment history. That the column agrees with "the tenant's approved
 * claim for this listing" is asserted elsewhere, not enforced here.
 *
 * Rollback:
 *   drop index if exists listings_owner;
 *   alter table listings drop constraint if exists listings_owner_user_fk;
 *   alter table listings drop column if exists owner_user_id;
 *   Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types.js";

export async function up(db: MigrationDb): Promise<void> {
  await sql`alter table listings add column owner_user_id text`.execute(db);

  await sql`
    alter table listings
      add constraint listings_owner_user_fk
        foreign key (tenant_id, owner_user_id)
        references users (tenant_id, id)
        on delete restrict
  `.execute(db);

  await sql`
    create index listings_owner
      on listings (tenant_id, owner_user_id)
      where owner_user_id is not null
  `.execute(db);
}
