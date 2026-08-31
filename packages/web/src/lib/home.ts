import { sql } from "@osds/db";
import { getDb } from "./db";

interface CategoryRow {
  slug: string;
  name: string;
  published_count: string | number;
}

export interface HomeCategory {
  readonly slug: string;
  readonly name: string;
  readonly publishedCount: number;
}

export interface HomePage {
  readonly tenantName: string;
  /** Categories with at least one published listing, ordered by name. */
  readonly categories: readonly HomeCategory[];
}

/**
 * Homepage data for this tenant, or null when the tenant row is gone (-> 404).
 *
 * Same tenancy pattern as `getCategoryPage`: one transaction that sets
 * `app.tenant_id` first (transaction-local), so RLS on categories /
 * listing_categories / listings is enforced for the `osds_app` role. `tenants`
 * itself has no RLS, but reading its name inside the same transaction keeps the
 * pattern uniform.
 *
 * The inner joins drop any category with no published listing, so an empty
 * category never appears on the homepage.
 */
export async function getHomePage(tenantId: string): Promise<HomePage | null> {
  return getDb()
    .transaction()
    .execute(async (trx) => {
      await sql`select set_config('app.tenant_id', ${tenantId}, true)`.execute(trx);

      const tenant = await sql<{ name: string }>`
        select name from tenants where id = ${tenantId} limit 1
      `.execute(trx);

      const tenantRow = tenant.rows[0];
      if (tenantRow === undefined) return null;

      const { rows } = await sql<CategoryRow>`
        select
          c.slug,
          c.name,
          count(l.id) as published_count
        from categories c
        join listing_categories lc
          on lc.tenant_id = c.tenant_id and lc.category_id = c.id
        join listings l
          on l.tenant_id = lc.tenant_id
         and l.id = lc.listing_id
         and l.visibility = 'published'
        group by c.id, c.slug, c.name
        order by c.name
      `.execute(trx);

      return {
        tenantName: tenantRow.name,
        categories: rows.map((r) => ({
          slug: r.slug,
          name: r.name,
          publishedCount: Number(r.published_count),
        })),
      };
    });
}
