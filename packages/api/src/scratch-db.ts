/**
 * Shared setup for the db-backed `@osds/api` tests. Copied from
 * `packages/core/src/persist/scratch-db.ts` - the two packages test against a
 * real Postgres the same way and neither owns the other's test tree.
 *
 * A fresh Postgres cluster (CI) has `osds_app` NOLOGIN with no password -
 * migration 0013 creates it that way. Locally it can log in only because
 * docker-compose's init script sets a password. So a test that needs a real
 * `osds_app` connection (not just `SET ROLE osds_app` from the owner) has to
 * provision that login itself, from the owner, after migrating.
 *
 * The helper provisions a THROWAWAY login for CI. It does not manage
 * credentials on a cluster that already has them: `ALTER ROLE` would reset the
 * cluster-global password, silently changing it for anything else on that
 * cluster. So it only touches the role when `rolcanlogin` is false; if the role
 * can already log in it takes the password from `DATABASE_URL` (or the dev
 * default) and leaves the role alone. It is safe under the repo's
 * `fileParallelism: false` - test files run one at a time, so no two setups
 * race on the role.
 */
import { randomUUID } from "node:crypto";
import { Client } from "pg";
import { migrateToLatest } from "@osds/db";

/** The least-privilege role the app and worker connect as. */
const APP_ROLE = "osds_app";
/**
 * Password provisioned for {@link APP_ROLE} on a cluster where it cannot yet log
 * in, and the fallback when `DATABASE_URL` is unset. Matches
 * `infra/postgres/init/10-osds-app-role.sql`.
 */
const APP_ROLE_PASSWORD = "osds_dev_only";

export interface ScratchDb {
  /** The scratch database name, for {@link dropScratchDb}. */
  readonly name: string;
  /** Owner connection string (DDL, RLS bypass) for the scratch database. */
  readonly ownerUrl: string;
  /** `osds_app` connection string for the scratch database - guaranteed LOGIN. */
  readonly appUrl: string;
}

/** Resolve the owner/admin connection string the test suite runs against. */
export function adminUrl(): string {
  return (
    process.env.OSDS_TEST_DATABASE_URL ??
    process.env.DATABASE_URL_ADMIN ??
    "postgresql://osds:osds_dev_only@localhost:5432/postgres"
  );
}

/** True if the admin URL accepts a connection - db-backed suites skip otherwise. */
export async function pgReachable(url = adminUrl()): Promise<boolean> {
  const client = new Client({ connectionString: url });
  try {
    await client.connect();
    await client.end();
    return true;
  } catch {
    return false;
  }
}

/**
 * Create a uniquely named scratch database, migrate it as the owner, and make
 * sure `osds_app` can log in. Returns both connection strings; pass `name` to
 * {@link dropScratchDb} in `afterAll`.
 */
export async function createScratchDb(owner = adminUrl()): Promise<ScratchDb> {
  const name = `osds_test_${randomUUID().replace(/-/g, "")}`;

  await runOnce(owner, `create database ${name}`);

  const ownerUrl = withDatabase(owner, name);
  const { error } = await migrateToLatest(ownerUrl);
  if (error) throw error;

  // Only provision a login when the role has none. `ALTER ROLE` resets a
  // cluster-global password; on a cluster where `osds_app` can already log in
  // (docker-compose, or a real deployment) that would silently change it, so
  // there we just take the password the deployment configured.
  let appPassword = APP_ROLE_PASSWORD;
  if (await roleCanLogin(ownerUrl, APP_ROLE)) {
    appPassword = passwordFromDatabaseUrl() ?? APP_ROLE_PASSWORD;
  } else {
    await runOnce(
      ownerUrl,
      `alter role ${APP_ROLE} login password '${APP_ROLE_PASSWORD}'`,
    );
  }

  return { name, ownerUrl, appUrl: withRole(ownerUrl, APP_ROLE, appPassword) };
}

export async function dropScratchDb(
  name: string,
  owner = adminUrl(),
): Promise<void> {
  await runOnce(owner, `drop database if exists ${name} with (force)`);
}

async function runOnce(connectionString: string, query: string): Promise<void> {
  const client = new Client({ connectionString });
  await client.connect();
  try {
    await client.query(query);
  } finally {
    await client.end();
  }
}

async function roleCanLogin(
  connectionString: string,
  role: string,
): Promise<boolean> {
  const client = new Client({ connectionString });
  await client.connect();
  try {
    const res = await client.query<{ rolcanlogin: boolean }>(
      `select rolcanlogin from pg_roles where rolname = $1`,
      [role],
    );
    return res.rows[0]?.rolcanlogin === true;
  } finally {
    await client.end();
  }
}

/** The password field of `DATABASE_URL` (raw, decoded), or undefined. */
function passwordFromDatabaseUrl(): string | undefined {
  const url = process.env.DATABASE_URL;
  if (!url) return undefined;
  try {
    const encoded = new URL(url).password;
    return encoded ? decodeURIComponent(encoded) : undefined;
  } catch {
    return undefined;
  }
}

function withDatabase(url: string, name: string): string {
  const u = new URL(url);
  u.pathname = `/${name}`;
  return u.toString();
}

function withRole(url: string, role: string, password: string): string {
  const u = new URL(url);
  u.username = role;
  u.password = password;
  return u.toString();
}
