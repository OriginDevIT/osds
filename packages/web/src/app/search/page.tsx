import { notFound } from "next/navigation";
import { resolveTenantId } from "../../lib/tenant";
import { getSearchResults } from "../../lib/search";
import { parseNear } from "../../lib/near";
import { ListingRow } from "../../components/listing-row";

// Reads the query string and the database per request - never prerendered.
export const dynamic = "force-dynamic";

interface PageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

function firstValue(value: string | string[] | undefined): string | null {
  if (Array.isArray(value)) return value[0] ?? null;
  return value ?? null;
}

export default async function SearchPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const q = firstValue(params["q"]);
  const near = firstValue(params["near"]);
  const radiusRaw = firstValue(params["radius_km"]);

  const tenantId = await resolveTenantId();
  if (tenantId === null) notFound();

  // An invalid `near` is a 400 from middleware.ts before we get here; treat any
  // that slips through as no location filter.
  const parsed = parseNear(near);
  const coords = parsed.kind === "coords" ? { lat: parsed.lat, lon: parsed.lon } : null;

  const hasText = q !== null && q.trim() !== "";
  const searched = hasText || coords !== null;

  const parsedRadius = radiusRaw === null ? NaN : Number(radiusRaw);
  const radiusKm = Number.isFinite(parsedRadius) && parsedRadius > 0 ? parsedRadius : 25;

  const results = searched
    ? await getSearchResults(tenantId, { q, near: coords, radiusKm })
    : [];

  return (
    <main>
      <h1>Search</h1>

      <form method="get" action="/search">
        <p>
          <label>
            Keywords <input type="text" name="q" defaultValue={q ?? ""} />
          </label>
        </p>
        <p>
          <label>
            Near (lat,lon){" "}
            <input type="text" name="near" defaultValue={near ?? ""} />
          </label>
        </p>
        <p>
          <label>
            Radius km{" "}
            <input type="text" name="radius_km" defaultValue={radiusRaw ?? ""} />
          </label>
        </p>
        <p>
          <button type="submit">Search</button>
        </p>
      </form>

      {!searched ? null : results.length === 0 ? (
        <p>No results.</p>
      ) : (
        <ul>
          {results.map((result) => (
            <ListingRow
              key={result.slug}
              href={
                result.categorySlug !== null
                  ? `/${result.categorySlug}/${result.slug}`
                  : null
              }
              name={result.name}
              entitlementStatus={result.entitlementStatus}
              tier={result.tier}
              categories={result.categories}
              locality={result.locality}
            />
          ))}
        </ul>
      )}
    </main>
  );
}
