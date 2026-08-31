/**
 * Exercises the §11.2 command log against a real Postgres brought up by the
 * @osds/db migrations. Every persist entrypoint is wired through
 * begin/concludeCommandLog; this checks a row lands for every outcome, that a
 * crash mid-apply leaves an unconcluded row, that a concluded row is frozen,
 * and that the log is tenant-scoped.
 *
 * `db` is built from DATABASE_URL - the `osds_app` pool the app and worker
 * actually use, NOT the owner - so the RLS policies are exercised for real. A
 * separate `owner` handle (the admin URL) is used only to seed fixtures and to
 * assert that null-tenant rows are owner-visible.
 *
 * Skips cleanly when no database is reachable.
 */
import { randomUUID } from "node:crypto";
import { Client } from "pg";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createKysely, migrateToLatest, sql } from "@osds/db";
import type { OsdsCommand } from "@osds/adapter-kit";
import type { ClaimMethod } from "../command/claim.js";
import {
  persistClaimApprove,
  persistClaimSubmit,
  persistListingUpsert,
  type PersistDeps,
} from "./index.js";

const ADMIN_URL =
  process.env.OSDS_TEST_DATABASE_URL ??
  process.env.DATABASE_URL_ADMIN ??
  "postgresql://osds:osds_dev_only@localhost:5432/postgres";
const APP_URL =
  process.env.DATABASE_URL ??
  "postgresql://osds_app:osds_dev_only@localhost:5432/osds";

const scratchName = `osds_cmdlog_test_${randomUUID().replace(/-/g, "")}`;
const withDb = (url: string): string => {
  const u = new URL(url);
  u.pathname = `/${scratchName}`;
  return u.toString();
};
const scratchAdminUrl = withDb(ADMIN_URL);
const scratchAppUrl = withDb(APP_URL);

async function pgReachable(): Promise<boolean> {
  const client = new Client({ connectionString: ADMIN_URL });
  try {
    await client.connect();
    await client.end();
    return true;
  } catch {
    return false;
  }
}
const available = await pgReachable();
if (!available) {
  console.warn(
    `[persist/command-log.test] Postgres not reachable at ${ADMIN_URL} - skipping`,
  );
}

const ENABLED: readonly ClaimMethod[] = ["manual", "phone_otp", "domain_email"];
const consent = {
  contact_by_business: {
    granted: true,
    at: "2026-08-28T14:22:10.000Z",
    ip: "203.0.113.44",
    text_version: "consent-v3",
  },
};
const claimant = {
  name: "Dana Hoffman",
  email: "dana@hoffman.example",
  phone_e164: "+17735550142",
  role_claimed: "owner",
};

interface CmdLogRow {
  id: string;
  tenant_id: string | null;
  command: string;
  adapter_id: string | null;
  idempotency_key: string | null;
  trace_id: string | null;
  payload: unknown;
  outcome: string | null;
  problem: unknown;
  event_id: string | null;
  received_at: Date;
  concluded_at: Date | null;
}

const SELECT_LOG = sql`
  select id, tenant_id, command, adapter_id, idempotency_key, trace_id,
         payload, outcome, problem, event_id, received_at, concluded_at
  from command_log
`;

(available ? describe : describe.skip)("command log (real Postgres)", () => {
  let db: ReturnType<typeof createKysely>; // osds_app pool
  let owner: ReturnType<typeof createKysely>; // admin - seeding + owner reads

  let seq = 0;
  const newId = (): string =>
    `${Date.now().toString().padStart(15, "0")}${(seq++).toString().padStart(9, "0")}`;
  const FIXED_NOW = new Date("2026-08-31T12:00:00.000Z");
  const deps: PersistDeps = { now: () => FIXED_NOW, newId };

  function cmd(
    command: OsdsCommand["command"],
    adapter_id: string,
    payload: Record<string, unknown>,
    over: Partial<OsdsCommand> = {},
  ): OsdsCommand {
    return {
      command,
      idempotency_key: `k_${newId()}`,
      tenant_id: "tnt_a",
      adapter_id,
      trace_id: `tr_${newId()}`,
      payload,
      ...over,
    };
  }
  const listingCmd = (p: Record<string, unknown>, o?: Partial<OsdsCommand>) =>
    cmd("listing.upsert", "webhook", p, o);
  const submitCmd = (p: Record<string, unknown>, o?: Partial<OsdsCommand>) =>
    cmd("claim.submit", "gohighlevel", p, o);
  const approveCmd = (p: Record<string, unknown>, o?: Partial<OsdsCommand>) =>
    cmd("claim.approve", "admin-console", p, o);

  /** A read on the app pool, scoped to one tenant through RLS. */
  async function asTenant<T>(
    tenantId: string,
    fn: (trx: ReturnType<typeof createKysely>) => Promise<T>,
  ): Promise<T> {
    return db.transaction().execute(async (trx) => {
      await sql`set local role osds_app`.execute(trx);
      await sql`select set_config('app.tenant_id', ${tenantId}, true)`.execute(
        trx,
      );
      return fn(trx);
    });
  }

  async function logByKey(tenantId: string, key: string) {
    return asTenant(tenantId, async (trx) => {
      const res = await sql<CmdLogRow>`
        ${SELECT_LOG} where idempotency_key = ${key} order by id
      `.execute(trx);
      return [...res.rows];
    });
  }

  /** Read as the owner (RLS bypassed) - the only view of null-tenant rows. */
  async function logByKeyAsOwner(key: string) {
    const res = await sql<CmdLogRow>`
      ${SELECT_LOG} where idempotency_key = ${key} order by id
    `.execute(owner);
    return [...res.rows];
  }

  async function seedListing(
    id: string,
    tenantId: string,
    slug: string,
    status = "unclaimed",
  ) {
    await sql`
      insert into listings (id, tenant_id, slug, name, status)
      values (${id}, ${tenantId}, ${slug}, ${slug}, ${status})
    `.execute(owner);
  }
  async function seedUser(id: string, tenantId: string, email: string) {
    await sql`
      insert into users (id, tenant_id, email, name, role)
      values (${id}, ${tenantId}, ${email}, ${email}, 'owner')
    `.execute(owner);
  }
  async function claimIdFor(
    tenantId: string,
    listingId: string,
  ): Promise<string> {
    return asTenant(tenantId, async (trx) => {
      const res = await sql<{ id: string }>`
        select id from claims where listing_id = ${listingId} limit 1
      `.execute(trx);
      return res.rows[0]!.id;
    });
  }

  beforeAll(async () => {
    const admin = new Client({ connectionString: ADMIN_URL });
    await admin.connect();
    await admin.query(`create database ${scratchName}`);
    await admin.end();

    const { error } = await migrateToLatest(scratchAdminUrl);
    if (error) throw error;

    owner = createKysely(scratchAdminUrl);
    await sql`
      insert into tenants (id, slug, name) values
        ('tnt_a', 'tenant-a', 'Tenant A'),
        ('tnt_b', 'tenant-b', 'Tenant B')
    `.execute(owner);

    db = createKysely(scratchAppUrl);
  });

  afterAll(async () => {
    await db?.destroy();
    await owner?.destroy();
    const admin = new Client({ connectionString: ADMIN_URL });
    await admin.connect();
    await admin.query(`drop database if exists ${scratchName} with (force)`);
    await admin.end();
  });

  it("created: logs the attempt and concludes it", async () => {
    const c = listingCmd({ slug: "log-created", name: "Log Created" });
    const res = await persistListingUpsert(db, c, deps);
    expect(res.status).toBe("created");

    const rows = await logByKey("tnt_a", c.idempotency_key);
    expect(rows).toHaveLength(1);
    const row = rows[0]!;
    expect(row).toMatchObject({
      tenant_id: "tnt_a",
      command: "listing.upsert",
      adapter_id: "webhook",
      idempotency_key: c.idempotency_key,
      trace_id: c.trace_id,
      outcome: "created",
      event_id: res.status === "created" ? res.event_id : null,
      problem: null,
    });
    expect(row.id.startsWith("cmd_")).toBe(true);
    expect(row.payload).toEqual({ slug: "log-created", name: "Log Created" });
    expect(row.received_at.toISOString()).toBe("2026-08-31T12:00:00.000Z");
    expect(row.concluded_at?.toISOString()).toBe("2026-08-31T12:00:00.000Z");
  });

  it("updated / unchanged: each concludes with its own outcome", async () => {
    await persistListingUpsert(
      db,
      listingCmd({ slug: "log-upd", name: "One" }),
      deps,
    );

    const updCmd = listingCmd({ slug: "log-upd", name: "Two" });
    expect((await persistListingUpsert(db, updCmd, deps)).status).toBe(
      "updated",
    );
    expect((await logByKey("tnt_a", updCmd.idempotency_key))[0]!.outcome).toBe(
      "updated",
    );

    const noopCmd = listingCmd({ slug: "log-upd", name: "Two" });
    expect((await persistListingUpsert(db, noopCmd, deps)).status).toBe(
      "unchanged",
    );
    const noopRow = (await logByKey("tnt_a", noopCmd.idempotency_key))[0]!;
    expect(noopRow.outcome).toBe("unchanged");
    expect(noopRow.event_id).toBeNull();
    expect(noopRow.problem).toBeNull();
  });

  it("rejected: the row carries the problem document, no event id", async () => {
    const c = listingCmd({ slug: "log-rej", name: "X", tier: "featured" });
    const res = await persistListingUpsert(db, c, deps);
    expect(res.status).toBe("rejected");

    const row = (await logByKey("tnt_a", c.idempotency_key))[0]!;
    expect(row.outcome).toBe("rejected");
    expect(row.event_id).toBeNull();
    expect(row.problem).toEqual(expect.objectContaining({ status: 422 }));
    expect(row.concluded_at).not.toBeNull();
  });

  it("duplicate: both attempts are logged, the replay concludes as duplicate", async () => {
    const c = listingCmd({ slug: "log-dup", name: "Dup" });
    const first = await persistListingUpsert(db, c, deps);
    const second = await persistListingUpsert(db, c, deps);
    expect(first.status).toBe("created");
    expect(second.status).toBe("duplicate");

    const rows = await logByKey("tnt_a", c.idempotency_key);
    expect(rows).toHaveLength(2);
    expect(rows.map((r) => r.outcome)).toEqual(["created", "duplicate"]);
    const eventId = first.status === "created" ? first.event_id : "";
    expect(rows[0]!.event_id).toBe(eventId);
    expect(rows[1]!.event_id).toBe(eventId);
  });

  it("claim outcomes: submitted, disputed and approved each conclude", async () => {
    await seedListing("listing_cl1", "tnt_a", "cl-1");
    await seedListing("listing_cl2", "tnt_a", "cl-2", "claimed");
    await seedUser("usr_cladmin", "tnt_a", "cladmin@x.example");

    const subCmd = submitCmd({
      listing_id: "listing_cl1",
      method: "phone_otp",
      claimant,
      consent,
    });
    expect((await persistClaimSubmit(db, subCmd, deps, ENABLED)).status).toBe(
      "submitted",
    );
    expect((await logByKey("tnt_a", subCmd.idempotency_key))[0]!.outcome).toBe(
      "submitted",
    );

    const dispCmd = submitCmd({
      listing_id: "listing_cl2",
      method: "phone_otp",
      claimant: { ...claimant, email: "disp@x.example" },
      consent,
    });
    expect((await persistClaimSubmit(db, dispCmd, deps, ENABLED)).status).toBe(
      "disputed",
    );
    expect((await logByKey("tnt_a", dispCmd.idempotency_key))[0]!.outcome).toBe(
      "disputed",
    );

    const claimId = await claimIdFor("tnt_a", "listing_cl1");
    const appCmd = approveCmd({ claim_id: claimId, decided_by: "usr_cladmin" });
    expect((await persistClaimApprove(db, appCmd, deps)).status).toBe(
      "approved",
    );
    const appRow = (await logByKey("tnt_a", appCmd.idempotency_key))[0]!;
    expect(appRow.outcome).toBe("approved");
    expect(appRow.command).toBe("claim.approve");
    expect(appRow.event_id).not.toBeNull();
  });

  it("a command that throws mid-apply leaves a row with a null outcome", async () => {
    await seedListing("listing_crash", "tnt_a", "crash");
    const subCmd = submitCmd({
      listing_id: "listing_crash",
      method: "phone_otp",
      claimant: { ...claimant, email: "crash@x.example" },
      consent,
    });
    await persistClaimSubmit(db, subCmd, deps, ENABLED);
    const claimId = await claimIdFor("tnt_a", "listing_crash");

    // decided_by references no user -> the update inside the command
    // transaction hits a foreign-key violation and throws.
    const appCmd = approveCmd({ claim_id: claimId, decided_by: "usr_ghost" });
    await expect(persistClaimApprove(db, appCmd, deps)).rejects.toThrow();

    const row = (await logByKey("tnt_a", appCmd.idempotency_key))[0]!;
    expect(row.command).toBe("claim.approve");
    expect(row.outcome).toBeNull();
    expect(row.concluded_at).toBeNull();
    expect(row.received_at.toISOString()).toBe("2026-08-31T12:00:00.000Z");
  });

  it("a concluded row is frozen - re-concluding it affects nothing", async () => {
    const c = listingCmd({ slug: "log-frozen", name: "Frozen" });
    await persistListingUpsert(db, c, deps);

    const row = (await logByKey("tnt_a", c.idempotency_key))[0]!;
    expect(row.outcome).toBe("created");

    const affected = await asTenant("tnt_a", async (trx) => {
      const r = await sql`
        update command_log set outcome = 'tampered', problem = null
        where id = ${row.id}
      `.execute(trx);
      return Number(r.numAffectedRows ?? 0n);
    });
    expect(affected).toBe(0);

    const after = (await logByKey("tnt_a", c.idempotency_key))[0]!;
    expect(after.outcome).toBe("created");
    expect(after.concluded_at?.toISOString()).toBe(
      row.concluded_at?.toISOString(),
    );
  });

  it("an unresolvable tenant: one already-concluded null-tenant row, owner-visible only", async () => {
    const c = listingCmd(
      { slug: "log-notenant", name: "No Tenant" },
      { tenant_id: "tnt_ghost" },
    );
    const res = await persistListingUpsert(db, c, deps);
    expect(res.status).toBe("rejected");

    // osds_app - any tenant GUC - cannot see it.
    expect(await logByKey("tnt_a", c.idempotency_key)).toHaveLength(0);
    expect(await logByKey("tnt_b", c.idempotency_key)).toHaveLength(0);

    // The owner can, and it is already concluded.
    const rows = await logByKeyAsOwner(c.idempotency_key);
    expect(rows).toHaveLength(1);
    const row = rows[0]!;
    expect(row.tenant_id).toBeNull();
    expect(row.command).toBe("listing.upsert");
    expect(row.outcome).toBe("rejected");
    expect(row.concluded_at).not.toBeNull();
    expect(String((row.problem as { detail?: string }).detail)).toContain(
      "unresolvable tenant",
    );
  });

  it("cross-tenant: a session sees only its own tenant's command log", async () => {
    const aCmd = listingCmd({ slug: "xt-a", name: "XT A" });
    const bCmd = listingCmd(
      { slug: "xt-b", name: "XT B" },
      { tenant_id: "tnt_b" },
    );
    await persistListingUpsert(db, aCmd, deps);
    await persistListingUpsert(db, bCmd, deps);

    expect(await logByKey("tnt_a", aCmd.idempotency_key)).toHaveLength(1);
    expect(await logByKey("tnt_a", bCmd.idempotency_key)).toHaveLength(0);
    expect(await logByKey("tnt_b", bCmd.idempotency_key)).toHaveLength(1);
    expect(await logByKey("tnt_b", aCmd.idempotency_key)).toHaveLength(0);
  });
});
