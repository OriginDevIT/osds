/**
 * Migration CLI. Loads the repo-root .env (so it works from a clean shell with
 * nothing exported), then applies every pending migration as the table owner
 * via DATABASE_URL_ADMIN (falling back to DATABASE_URL).
 *
 *   pnpm --filter @osds/db migrate      # or, from the repo root: pnpm migrate:dev
 */
import * as path from "node:path";
import { loadEnvFile } from "node:process";
import { migrateToLatest } from "./migrator.js";

// packages/db/src (or dist) -> repo root. Anchored to this file, not the cwd,
// because `pnpm --filter` runs with the package as the working directory.
try {
  loadEnvFile(path.resolve(import.meta.dirname, "../../../.env"));
} catch {
  // No .env at the repo root - rely on the ambient environment.
}

const connectionString = process.env.DATABASE_URL_ADMIN ?? process.env.DATABASE_URL;
if (!connectionString) {
  throw new Error("DATABASE_URL_ADMIN (or DATABASE_URL) is not set");
}

const { error, results } = await migrateToLatest(connectionString);

for (const r of results ?? []) {
  const outcome =
    r.status === "Success" ? "applied" : r.status === "Error" ? "FAILED " : "skipped";
  console.log(`${outcome}  ${r.migrationName}`);
}

if (error) {
  console.error(error);
  process.exitCode = 1;
}
