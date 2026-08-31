import { notFound } from "next/navigation";
import { resolveTenantId } from "../lib/tenant";
import { getHomePage } from "../lib/home";

// Depends on the Host header and a per-request database read - never prerendered.
export const dynamic = "force-dynamic";

export default async function HomePage() {
  const tenantId = await resolveTenantId();
  if (tenantId === null) notFound();

  const home = await getHomePage(tenantId);
  if (home === null) notFound();

  return (
    <main>
      <h1>{home.tenantName}</h1>

      {home.categories.length === 0 ? (
        <p>No categories.</p>
      ) : (
        <ul>
          {home.categories.map((category) => (
            <li key={category.slug}>
              <a href={`/${category.slug}`}>{category.name}</a> ({category.publishedCount})
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
