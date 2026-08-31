import { sql } from "@osds/db";
import type { EntitlementStatus } from "@osds/core";
import { getDb } from "./db";

interface ListingRow {
  name: string;
  listing_tier: string | null;
  address_line1: string | null;
  address_line2: string | null;
  locality: string | null;
  region: string | null;
  postal_code: string | null;
  country: string | null;
  contact_phone_e164: string | null;
  contact_email: string | null;
  contact_website: string | null;
  entitlement_status: EntitlementStatus | null;
  entitlement_tier: string | null;
  category_names: string[] | null;
}

export interface ListingView {
  readonly name: string;
  readonly categoryNames: readonly string[];
  readonly address: readonly string[];
  readonly phone: string | null;
  readonly email: string | null;
  readonly website: string | null;
  /** For the §6.5 resolver; `none` when the listing has no entitlement row. */
  readonly entitlementStatus: EntitlementStatus;
  /** Tier name to show *if* the resolver says the badge is visible. */
  readonly tier: string | null;
}

/**
 * The one published listing at `/{categorySlug}/{listingSlug}` for this tenant,
 * or null (route to a 404) when it does not exist, is not in that category, or
 * is not `visibility = 'published'`.
 *
 * The whole read runs in one transaction that first sets `app.tenant_id`
 * (transaction-local), so RLS on listings / listing_categories / categories /
 * entitlements is enforced for the `osds_app` role.
 */
export async function getPublishedListing(
  tenantId: string,
  categorySlug: string,
  listingSlug: string,
): Promise<ListingView | null> {
  const row = await getDb()
    .transaction()
    .execute(async (trx) => {
      await sql`select set_config('app.tenant_id', ${tenantId}, true)`.execute(trx);

      const { rows } = await sql<ListingRow>`
        select
          l.name,
          l.tier             as listing_tier,
          l.address_line1,
          l.address_line2,
          l.locality,
          l.region,
          l.postal_code,
          l.country,
          l.contact_phone_e164,
          l.contact_email,
          l.contact_website,
          ent.status         as entitlement_status,
          ent.tier           as entitlement_tier,
          cats.names         as category_names
        from listings l
        join listing_categories lc
          on lc.tenant_id = l.tenant_id and lc.listing_id = l.id
        join categories route_cat
          on route_cat.tenant_id = lc.tenant_id
         and route_cat.id = lc.category_id
         and route_cat.slug = ${categorySlug}
        left join lateral (
          select array_agg(c.name order by c.name) as names
          from listing_categories x
          join categories c
            on c.tenant_id = x.tenant_id and c.id = x.category_id
          where x.tenant_id = l.tenant_id and x.listing_id = l.id
        ) cats on true
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
        where l.slug = ${listingSlug}
          and l.visibility = 'published'
        limit 1
      `.execute(trx);

      return rows[0] ?? null;
    });

  if (row === null) return null;

  const cityLine = [row.locality, row.region, row.postal_code]
    .filter((p): p is string => p !== null && p.trim() !== "")
    .join(" ");

  const address = [row.address_line1, row.address_line2, cityLine, row.country].filter(
    (line): line is string => line !== null && line.trim() !== "",
  );

  return {
    name: row.name,
    categoryNames: row.category_names ?? [],
    address,
    phone: row.contact_phone_e164,
    email: row.contact_email,
    website: row.contact_website,
    entitlementStatus: row.entitlement_status ?? "none",
    tier: row.entitlement_tier ?? row.listing_tier,
  };
}
