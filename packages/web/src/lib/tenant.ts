import { headers } from "next/headers";
import { sql } from "@osds/db";
import { getDb } from "./db";

interface TenantRow {
  id: string;
}

const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

/** Host header value -> bare hostname (port stripped, IPv6 brackets kept). */
function hostname(header: string | null): string | null {
  if (header === null) return null;
  const value = header.trim().toLowerCase();
  if (value === "") return null;
  if (value.startsWith("[")) {
    const close = value.indexOf("]");
    return close === -1 ? value : value.slice(0, close + 1);
  }
  const colon = value.indexOf(":");
  return colon === -1 ? value : value.slice(0, colon);
}

/**
 * Resolve the tenant for this request.
 *
 * Primary: the Host header matched against `tenants.domain`.
 * Fallback: `OSDS_DEV_TENANT_SLUG` matched against `tenants.slug`, but only when
 * the request is to a loopback host - so an unknown production domain 404s
 * rather than silently serving the dev tenant.
 *
 * Returns null when nothing matches; the caller renders a 404.
 *
 * `tenants` carries no `tenant_id` and no RLS (migration 0002), so this query
 * runs without an `app.tenant_id` scope.
 */
export async function resolveTenantId(): Promise<string | null> {
  const host = hostname((await headers()).get("host"));
  const isLoopback =
    host === null || LOOPBACK.has(host) || host.endsWith(".localhost");
  const devSlug = isLoopback
    ? (process.env.OSDS_DEV_TENANT_SLUG?.toLowerCase() ?? null)
    : null;

  if (host === null && devSlug === null) return null;

  const { rows } = await sql<TenantRow>`
    select id
    from tenants
    where domain = ${host}
       or slug = ${devSlug}
    order by (domain = ${host}) desc nulls last
    limit 1
  `.execute(getDb());

  return rows[0]?.id ?? null;
}
