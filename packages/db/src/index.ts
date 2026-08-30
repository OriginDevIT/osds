/**
 * @osds/db - Postgres access for OSDS.
 *
 * The schema is defined by the SQL migrations in ./migrations and owned by the
 * database. Generated row types land in ./schema.ts after
 * `pnpm --filter @osds/db codegen` run against a migrated database; pass that
 * `DB` type to `createKysely<DB>()`.
 */
import { Kysely, PostgresDialect } from "kysely";
import { Pool } from "pg";

export { sql } from "kysely";
export type { Kysely } from "kysely";

/** Build a Kysely instance backed by a `pg` pool. Throws if `DATABASE_URL` is unset. */
export function createKysely<DB = unknown>(
  connectionString: string | undefined = process.env.DATABASE_URL,
): Kysely<DB> {
  if (!connectionString) {
    throw new Error("DATABASE_URL is not set");
  }
  return new Kysely<DB>({
    dialect: new PostgresDialect({ pool: new Pool({ connectionString }) }),
  });
}
