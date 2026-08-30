import type { Kysely } from "kysely";

/**
 * Migrations issue raw DDL and run before any generated schema types exist, so
 * they operate on an untyped Kysely instance.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type MigrationDb = Kysely<any>;
