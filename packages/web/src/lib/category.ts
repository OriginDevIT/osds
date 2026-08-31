import { sql } from "@osds/db";
import type { EntitlementStatus } from "@osds/core";
import { getDb } from "./db";

interface ListingRow {
  slug: string;
  name: string;
  listing_tier: string | null;
  entitlement_status: EntitlementStatus | null;
  entitlement_tier: string | null;
}

export interface CategoryListing {
  readonly slug: string;
  readonly name: string;
  /** For the §6.5 resolver; `none` when the listing has no entitlement row. */
  readonly entitlementStatus: EntitlementStatus;
  /** Tier name to show *if* the resolver says the badge is visible. */
  readonly tier: string | null;
}

export interface CategoryPage {
  readonly name: string;
  /** Every published listing in the category, ordered by name. Possibly empty. */
  readonly listings: readonly CategoryListing[];
}

/**
 * The category page data for this tenant, or null when the category slug does
 * not exist (-> 404). A category that exists but has no published listings
 * returns an empty `listings` array.
 *
 * Same tenancy pattern as `getPublishedListing`: one transaction that sets
 * `app.tenant_id` first (transaction-local), so RLS on categories /
 * listing_categories / listings / entitlements is enforced for `osds_app`.
 *
 * Featured-placement ordering is applied by the caller with the §6.5 resolver;
 * here the rows come back name-ordered so the caller's partition stays
 * alphabetical within each group.
 */
export async function getCategoryPage(
  tenantId: string,
  categorySlug: string,
): Promise<CategoryPage | null> {
  return getDb()
    .transaction()
    .execute(async (trx) => {
      await sql`select set_config('app.tenant_id', ${tenantId}, true)`.execute(trx);

      const category = await sql<{ name: string }>`
        select name from categories where slug = ${categorySlug} limit 1
      `.execute(trx);

      const categoryRow = category.rows[0];
      if (categoryRow === undefined) return null;

      const { rows } = await sql<ListingRow>`
        select
          l.slug,
          l.name,
          l.tier         as listing_tier,
          ent.status     as entitlement_status,
          ent.tier       as entitlement_tier
        from listings l
        join listing_categories lc
          on lc.tenant_id = l.tenant_id and lc.listing_id = l.id
        join categories c
          on c.tenant_id = lc.tenant_id
         and c.id = lc.category_id
         and c.slug = ${categorySlug}
        left join lateral (
          -- The listing's current entitlement. The partial unique index allows
          -- at most one non-terminal row; this ordering also picks sensibly if a
          -- terminal (expired / canceled) row sits alongside it.
          select e.status, e.tier
          from entitlements e
          where e.tenant_id = l.tenant_id and e.listing_id = l.id
          order by
            case e.status
              when 'active'   then 0
              when 'trialing' then 1
              when 'past_due'  then 2
              when 'comped'   then 3
              when 'canceled' then 4
              when 'grace'    then 5
              when 'expired'  then 6
              else 7
            end,
            e.created_at desc
          limit 1
        ) ent on true
        where l.visibility = 'published'
        order by l.name
      `.execute(trx);

      return {
        name: categoryRow.name,
        listings: rows.map((r) => ({
          slug: r.slug,
          name: r.name,
          entitlementStatus: r.entitlement_status ?? "none",
          tier: r.entitlement_tier ?? r.listing_tier,
        })),
      };
    });
}
