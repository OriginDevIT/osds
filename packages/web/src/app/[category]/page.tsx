import { notFound } from "next/navigation";
import { resolvePublicRender } from "@osds/core";
import { resolveTenantId } from "../../lib/tenant";
import { getCategoryPage } from "../../lib/category";

// Depends on the Host header and a per-request database read - never prerendered.
export const dynamic = "force-dynamic";

interface PageProps {
  params: Promise<{ category: string }>;
}

export default async function CategoryPage({ params }: PageProps) {
  const { category } = await params;

  const tenantId = await resolveTenantId();
  if (tenantId === null) notFound();

  const page = await getCategoryPage(tenantId, category);
  if (page === null) notFound();

  // §6.5: the resolver owns featured placement and badge visibility. We only
  // read its answer - featuredPlacement to group, badge to decide the label.
  const rendered = page.listings.map((listing) => ({
    listing,
    render: resolvePublicRender(listing.entitlementStatus),
  }));

  // Featured placement first, then the rest. `page.listings` is already
  // name-ordered, so each group stays alphabetical.
  const ordered = [
    ...rendered.filter((r) => r.render.featuredPlacement),
    ...rendered.filter((r) => !r.render.featuredPlacement),
  ];

  return (
    <main>
      <h1>{page.name}</h1>

      {ordered.length === 0 ? (
        <p>No listings.</p>
      ) : (
        <ul>
          {ordered.map(({ listing, render }) => (
            <li key={listing.slug}>
              <a href={`/${category}/${listing.slug}`}>{listing.name}</a>
              {render.badge === "tier" && listing.tier !== null ? (
                <span> {listing.tier}</span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
