/**
 * Migration runner. Forward-only: applies every pending migration in
 * ./migrations, in filename order, connecting as the table owner
 * (DATABASE_URL_ADMIN, falling back to DATABASE_URL for single-URL setups).
 *
 *   pnpm --filter @osds/db migrate      # or, from the repo root: pnpm migrate:dev
 *
 * Loads the repo-root .env first, so it works from a clean shell with nothing
 * exported. A real environment (CI, production) sets the vars directly and
 * needs no .env.
 */
import { promises as fs } from "node:fs";
import * as path from "node:path";
import { loadEnvFile } from "node:process";
import { pathToFileURL } from "node:url";
import {
  Kysely,
  Migrator,
  PostgresDialect,
  type Migration,
  type MigrationProvider,
} from "kysely";
import { Pool } from "pg";

// packages/db/src (or dist) -> repo root. Anchored to this file, not the cwd,
// because `pnpm --filter` runs with the package as the working directory.
const repoRootEnv = path.resolve(import.meta.dirname, "../../../.env");
try {
  loadEnvFile(repoRootEnv);
} catch {
  // No .env at the repo root - rely on the ambient environment.
}

const migrationFolder = path.join(import.meta.dirname, "migrations");

/** Loads ./migrations/NNNN_*.{ts,js} via file:// URLs so it also works on Windows. */
const provider: MigrationProvider = {
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

async function main(): Promise<void> {
  const connectionString = process.env.DATABASE_URL_ADMIN ?? process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error("DATABASE_URL_ADMIN (or DATABASE_URL) is not set");
  }

  // Untyped on purpose: the runner operates on the raw schema, before codegen.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const db = new Kysely<any>({
    dialect: new PostgresDialect({ pool: new Pool({ connectionString }) }),
  });

  try {
    const { error, results } = await new Migrator({ db, provider }).migrateToLatest();

    for (const r of results ?? []) {
      const outcome =
        r.status === "Success" ? "applied" : r.status === "Error" ? "FAILED " : "skipped";
      console.log(`${outcome}  ${r.migrationName}`);
    }

    if (error) {
      console.error(error);
      process.exitCode = 1;
    }
  } finally {
    await db.destroy();
  }
}

await main();
