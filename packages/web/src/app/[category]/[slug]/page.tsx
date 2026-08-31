import { notFound } from "next/navigation";
import { resolvePublicRender } from "@osds/core";
import { resolveTenantId } from "../../../lib/tenant";
import { getPublishedListing } from "../../../lib/listing";

// Depends on the Host header and a per-request database read - never prerendered.
export const dynamic = "force-dynamic";

interface PageProps {
  params: Promise<{ category: string; slug: string }>;
}

export default async function ListingPage({ params }: PageProps) {
  const { category, slug } = await params;

  const tenantId = await resolveTenantId();
  if (tenantId === null) notFound();

  const listing = await getPublishedListing(tenantId, category, slug);
  if (listing === null) notFound();

  // §6.5: the resolver owns tier display. We only decide whether to show the
  // badge from its answer - we do not re-derive it from entitlement fields.
  const { badge } = resolvePublicRender(listing.entitlementStatus);
  const tierBadge = badge === "tier" ? listing.tier : null;

  return (
    <main>
      <h1>{listing.name}</h1>

      {tierBadge !== null ? <p>{tierBadge}</p> : null}

      {listing.categoryNames.length > 0 ? (
        <p>{listing.categoryNames.join(", ")}</p>
      ) : null}

      {listing.address.length > 0 ? (
        <address>
          {listing.address.map((line) => (
            <div key={line}>{line}</div>
          ))}
        </address>
      ) : null}

      {listing.phone !== null ? (
        <p>
          <a href={`tel:${listing.phone}`}>{listing.phone}</a>
        </p>
      ) : null}

      {listing.email !== null ? (
        <p>
          <a href={`mailto:${listing.email}`}>{listing.email}</a>
        </p>
      ) : null}

      {listing.website !== null ? (
        <p>
          <a href={listing.website} rel="nofollow noopener">
            {listing.website}
          </a>
        </p>
      ) : null}
    </main>
  );
}
