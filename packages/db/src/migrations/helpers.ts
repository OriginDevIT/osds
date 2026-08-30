import { sql } from "kysely";
import type { MigrationDb } from "./types.js";

/**
 * Tenant isolation for every table except `tenants`: RLS enabled and forced (so
 * the table owner is constrained too) plus a policy scoping every row to
 * `app.tenant_id`. When the session var is unset the policy yields no rows -
 * default deny.
 */
export async function enableTenantRls(db: MigrationDb, table: string): Promise<void> {
  await sql`alter table ${sql.ref(table)} enable row level security`.execute(db);
  await sql`alter table ${sql.ref(table)} force row level security`.execute(db);
  await sql`
    create policy tenant_isolation on ${sql.ref(table)}
      using (tenant_id = osds_current_tenant_id())
      with check (tenant_id = osds_current_tenant_id())
  `.execute(db);
}

/** Attach the shared BEFORE UPDATE trigger that maintains `updated_at`. */
export async function touchUpdatedAt(db: MigrationDb, table: string): Promise<void> {
  await sql`
    create trigger ${sql.ref(`${table}_touch_updated_at`)}
      before update on ${sql.ref(table)}
      for each row execute function osds_set_updated_at()
  `.execute(db);
}
