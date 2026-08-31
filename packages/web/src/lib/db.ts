import { createKysely } from "@osds/db";
import type { Kysely } from "@osds/db";

/**
 * One Kysely pool per server process, reused across requests. `next dev`
 * re-evaluates modules on hot reload, so the instance is stashed on globalThis
 * to avoid leaking pools.
 *
 * `createKysely()` reads `process.env.DATABASE_URL` - the least-privilege
 * `osds_app` role (see .env / .env.example), *not* `DATABASE_URL_ADMIN`. That
 * role holds no BYPASSRLS, so row-level security is enforced once the query
 * transaction sets `app.tenant_id`.
 */
const globalForDb = globalThis as typeof globalThis & {
  __osdsWebDb?: Kysely<unknown>;
};

export function getDb(): Kysely<unknown> {
  return (globalForDb.__osdsWebDb ??= createKysely());
}
