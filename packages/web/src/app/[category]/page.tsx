import { notFound } from "next/navigation";
import { resolvePublicRender } from "@osds/core";
import { resolveTenantId } from "../../lib/tenant";
import { getCategoryPage } from "../../lib/category";
import { ListingRow } from "../../components/listing-row";

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

  // §6.5: featured placement first, then the rest. `page.listings` is already
  // name-ordered, so each group stays alphabetical. ListingRow calls the same
  // resolver again for the badge.
  const isFeatured = (status: (typeof page.listings)[number]["entitlementStatus"]) =>
    resolvePublicRender(status).featuredPlacement;
  const ordered = [
    ...page.listings.filter((l) => isFeatured(l.entitlementStatus)),
    ...page.listings.filter((l) => !isFeatured(l.entitlementStatus)),
  ];

  return (
    <main>
      <h1>{page.name}</h1>

      {ordered.length === 0 ? (
        <p>No listings.</p>
      ) : (
        <ul>
          {ordered.map((listing) => (
            <ListingRow
              key={listing.slug}
              href={`/${category}/${listing.slug}`}
              name={listing.name}
              entitlementStatus={listing.entitlementStatus}
              tier={listing.tier}
            />
          ))}
        </ul>
      )}
    </main>
  );
}
