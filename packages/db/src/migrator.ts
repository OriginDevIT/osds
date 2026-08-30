/**
 * Reusable migration engine. `migrate.ts` is the CLI around it; tests import
 * {@link migrateToLatest} to bring a scratch database up to date.
 */
import { promises as fs } from "node:fs";
import * as path from "node:path";
import { pathToFileURL } from "node:url";
import {
  Kysely,
  Migrator,
  PostgresDialect,
  type Migration,
  type MigrationProvider,
  type MigrationResultSet,
} from "kysely";
import { Pool } from "pg";

const migrationFolder = path.join(import.meta.dirname, "migrations");

/** Loads ./migrations/NNNN_*.{ts,js} via file:// URLs so it also works on Windows. */
export const migrationProvider: MigrationProvider = {
  async getMigrations(): Promise<Record<string, Migration>> {
    const entries = await fs.readdir(migrationFolder);
    const files = entries
      .filter((f) => /^\d{4}_.+\.(ts|js)$/.test(f) && !f.endsWith(".d.ts"))
      .sort();

    const migrations: Record<string, Migration> = {};
    for (const file of files) {
      const href = pathToFileURL(path.join(migrationFolder, file)).href;
      migrations[file.replace(/\.(ts|js)$/, "")] = (await import(href)) as Migration;
    }
    return migrations;
  },
};

/**
 * Apply every pending migration to `connectionString`. Forward-only. The caller
 * inspects the returned result set (`results`, `error`).
 */
export async function migrateToLatest(connectionString: string): Promise<MigrationResultSet> {
  // Untyped on purpose: migrations run against the raw schema, before codegen.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const db = new Kysely<any>({
    dialect: new PostgresDialect({ pool: new Pool({ connectionString }) }),
  });

  try {
    return await new Migrator({ db, provider: migrationProvider }).migrateToLatest();
  } finally {
    await db.destroy();
  }
}
