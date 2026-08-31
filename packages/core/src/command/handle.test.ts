/**
 * Exercises handleCommand against a real Postgres brought up by the @osds/db
 * migrations. Skips cleanly when no database is reachable (e.g. CI without a
 * Postgres service).
 *
 * Point it at a server with `OSDS_TEST_DATABASE_URL` or `DATABASE_URL_ADMIN`;
 * defaults to the docker-compose dev instance. It creates and drops a uniquely
 * named scratch database per run.
 */
import { randomUUID } from "node:crypto";
import { Client } from "pg";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createKysely, migrateToLatest, sql } from "@osds/db";
import type { OsdsCommand, Scope } from "@osds/adapter-kit";
import { handleCommand, type CommandContext } from "../persist/index.js";

const ADMIN_URL =
  process.env.OSDS_TEST_DATABASE_URL ??
  process.env.DATABASE_URL_ADMIN ??
  "postgresql://osds:osds_dev_only@localhost:5432/postgres";

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

const scratchName = `osds_cmd_test_${randomUUID().replace(/-/g, "")}`;
const scratchUrl = ((): string => {
  const u = new URL(ADMIN_URL);
  u.pathname = `/${scratchName}`;
  return u.toString();
})();

const available = await pgReachable();
if (!available) {
  console.warn(`[handle.test] Postgres not reachable at ${ADMIN_URL} - skipping`);
}

(available ? describe : describe.skip)("entitlement command handler (real Postgres)", () => {
  let db: ReturnType<typeof createKysely>;

  // Monotonic ids, so `order by id` on the outbox reflects insert order the way
  // production ULIDs do. `randomUUID` (unsortable) is fine only where uniqueness
  // is all that matters.
  let seq = 0;
  const newId = (): string =>
    `${Date.now().toString().padStart(15, "0")}${(seq++).toString().padStart(9, "0")}`;
  const FIXED_NOW = new Date("2026-08-30T12:00:00.000Z");

  // `db` is assigned in beforeAll; read it lazily so contexts built during
  // collection still see it.
  const ctx = (scopes: readonly Scope[]): CommandContext => ({
    get db() {
      return db;
    },
    scopes,
    newId,
    now: () => FIXED_NOW,
  });
  const withScope = ctx(["command:entitlement"]);

  async function makeListing(): Promise<string> {
    const id = `listing_${newId()}`;
    await sql`
      insert into listings (id, tenant_id, slug, name)
      values (${id}, 'tnt_t', ${id}, 'L')
    `.execute(db);
    return id;
  }

  function reportPayment(
    listingId: string,
    payload: Record<string, unknown>,
    over: Partial<OsdsCommand> = {},
  ): OsdsCommand {
    return {
      command: "entitlement.reportPayment",
      idempotency_key: `k_${newId()}`,
      tenant_id: "tnt_t",
      adapter_id: "stripe",
      trace_id: `tr_${newId()}`,
      payload: { listing_id: listingId, ...payload },
      ...over,
    };
  }

  const succeededPayload = {
    outcome: "succeeded",
    tier: "featured",
    period_end: "2026-09-30T00:00:00.000Z",
    external_id: "sub_1",
  };

  async function liveEntitlement(listingId: string) {
    const res = await sql<{ status: string; tier: string }>`
      select status, tier from entitlements
      where tenant_id = 'tnt_t' and listing_id = ${listingId}
        and status not in ('expired', 'canceled')
    `.execute(db);
    return res.rows[0] ?? null;
  }

  async function listingTier(listingId: string): Promise<string | null> {
    const res = await sql<{ tier: string | null }>`
      select tier from listings where tenant_id = 'tnt_t' and id = ${listingId}
    `.execute(db);
    return res.rows[0]?.tier ?? null;
  }

  async function outboxFor(listingId: string) {
    const res = await sql<{
      id: string;
      type: string;
      idempotency_key: string | null;
      actor: unknown;
      data: unknown;
      origin: string;
      trace_id: string;
    }>`
      select id, type, idempotency_key, actor, data, origin, trace_id
      from outbox where subject = ${listingId} order by id
    `.execute(db);
    return res.rows;
  }

  beforeAll(async () => {
    const admin = new Client({ connectionString: ADMIN_URL });
    await admin.connect();
    await admin.query(`create database ${scratchName}`);
    await admin.end();

    const { error } = await migrateToLatest(scratchUrl);
    if (error) throw error;

    db = createKysely(scratchUrl);
    await sql`insert into tenants (id, slug, name) values ('tnt_t', 'tnt-t', 'Tenant T')`.execute(db);
    await sql`
      insert into tiers (tenant_id, key, rank, purchasable, uses_slot) values
        ('tnt_t', 'free', 0, false, false),
        ('tnt_t', 'featured', 2, true, true)
    `.execute(db);
  });

  afterAll(async () => {
    await db?.destroy();
    const admin = new Client({ connectionString: ADMIN_URL });
    await admin.connect();
    await admin.query(`drop database if exists ${scratchName} with (force)`);
    await admin.end();
  });

  it("reportPayment succeeded from none -> active, writes entitlement + outbox in one go", async () => {
    const listing = await makeListing();
    const cmd = reportPayment(listing, succeededPayload);

    const res = await handleCommand(cmd, withScope);

    expect(res.status).toBe("accepted");
    const eventId = res.status === "accepted" ? res.event_id : "";

    expect(await liveEntitlement(listing)).toEqual({ status: "active", tier: "featured" });
    expect(await listingTier(listing)).toBe("featured");

    const events = await outboxFor(listing);
    expect(events.map((e) => e.type)).toEqual([
      "entitlement.started",
      "listing.tier_changed",
    ]);
    expect(events[0]?.id).toBe(eventId);
    expect(events[0]?.idempotency_key).toBe(cmd.idempotency_key);
    expect(events[1]?.idempotency_key).toBeNull();
    expect(events[0]?.origin).toBe("stripe");
    expect(events[0]?.trace_id).toBe(cmd.trace_id);
  });

  it("replays an idempotency_key: 409 with the original event id, no second effect", async () => {
    const listing = await makeListing();
    const cmd = reportPayment(listing, succeededPayload);

    const first = await handleCommand(cmd, withScope);
    const second = await handleCommand(cmd, withScope);

    expect(first.status).toBe("accepted");
    expect(second).toEqual({
      status: "duplicate",
      event_id: first.status === "accepted" ? first.event_id : undefined,
    });

    const events = await outboxFor(listing);
    expect(events.filter((e) => e.idempotency_key !== null)).toHaveLength(1);
  });

  it("rejects with 403 when the adapter lacks command:entitlement, and writes nothing", async () => {
    const listing = await makeListing();
    const res = await handleCommand(reportPayment(listing, succeededPayload), ctx([]));

    expect(res.status).toBe("rejected");
    if (res.status === "rejected") {
      expect(res.problem.status).toBe(403);
      expect(res.problem.code).toBe("scope_denied");
    }
    expect(await liveEntitlement(listing)).toBeNull();
    expect(await outboxFor(listing)).toHaveLength(0);
  });

  it("rejects a malformed payload with 422", async () => {
    const cmd = reportPayment("listing_x", {});
    // strip listing_id
    (cmd.payload as Record<string, unknown>)["listing_id"] = undefined;

    const res = await handleCommand(cmd, withScope);

    expect(res.status).toBe("rejected");
    if (res.status === "rejected") expect(res.problem.status).toBe(422);
  });

  it("rejects an unhandled command name with 422", async () => {
    const res = await handleCommand(
      { ...reportPayment("listing_1", succeededPayload), command: "entitlement.revoke" },
      withScope,
    );
    expect(res.status).toBe("rejected");
    if (res.status === "rejected") {
      expect(res.problem.status).toBe(422);
      expect(String(res.problem.detail)).toContain("not handled");
    }
  });

  it("rejects a tier the tenant has not defined with 422", async () => {
    const listing = await makeListing();
    const res = await handleCommand(
      reportPayment(listing, { ...succeededPayload, tier: "platinum" }),
      withScope,
    );
    expect(res.status).toBe("rejected");
    if (res.status === "rejected") {
      expect(res.problem.status).toBe(422);
      expect(String(res.problem.detail)).toContain("platinum");
    }
  });

  it("reportPayment failed from active -> past_due, listing keeps full-tier perks (§6.5)", async () => {
    const listing = await makeListing();
    await handleCommand(reportPayment(listing, succeededPayload), withScope);

    const res = await handleCommand(reportPayment(listing, { outcome: "failed" }), withScope);

    expect(res.status).toBe("accepted");
    expect(await liveEntitlement(listing)).toEqual({ status: "past_due", tier: "featured" });
    expect(await listingTier(listing)).toBe("featured");
    const types = (await outboxFor(listing)).map((e) => e.type);
    expect(types).toContain("entitlement.dunning_started");
  });

  it("reportPayment refunded from active -> expired, listing drops to the rank-0 tier", async () => {
    const listing = await makeListing();
    await handleCommand(reportPayment(listing, succeededPayload), withScope);

    const res = await handleCommand(reportPayment(listing, { outcome: "refunded" }), withScope);

    expect(res.status).toBe("accepted");
    expect(await liveEntitlement(listing)).toBeNull();
    expect(await listingTier(listing)).toBe("free");
    const last = (await outboxFor(listing)).at(-1);
    expect(last?.type).toBe("listing.tier_changed");
    expect(last?.idempotency_key).not.toBeNull();
  });

  it("reportPayment succeeded from active (renewal) is rejected 422 - not one of the two commands' transitions", async () => {
    const listing = await makeListing();
    await handleCommand(reportPayment(listing, succeededPayload), withScope);

    const res = await handleCommand(reportPayment(listing, succeededPayload), withScope);

    expect(res.status).toBe("rejected");
    if (res.status === "rejected") {
      expect(res.problem.status).toBe(422);
      expect(String(res.problem.detail)).toContain("active");
    }
  });

  it("entitlement.grant -> comped at the granted tier, emits entitlement.overridden as an admin actor", async () => {
    const listing = await makeListing();
    const cmd: OsdsCommand = {
      command: "entitlement.grant",
      idempotency_key: `k_${newId()}`,
      tenant_id: "tnt_t",
      adapter_id: "admin-console",
      trace_id: `tr_${newId()}`,
      payload: {
        listing_id: listing,
        tier: "featured",
        admin_id: "usr_admin_9",
        reason: "goodwill credit",
      },
    };

    const res = await handleCommand(cmd, withScope);

    expect(res.status).toBe("accepted");
    expect(await liveEntitlement(listing)).toEqual({ status: "comped", tier: "featured" });

    const events = await outboxFor(listing);
    expect(events).toHaveLength(1);
    expect(events[0]?.type).toBe("entitlement.overridden");
    expect(events[0]?.actor).toEqual({ type: "admin", id: "usr_admin_9" });
    expect(events[0]?.data).toMatchObject({ admin_id: "usr_admin_9", reason: "goodwill credit" });
  });

  it("concurrent identical commands: exactly one apply, the other returns the same event id (409)", async () => {
    const listing = await makeListing();
    const cmd = reportPayment(listing, succeededPayload);

    const [a, b] = await Promise.all([
      handleCommand(cmd, withScope),
      handleCommand(cmd, withScope),
    ]);

    const statuses = [a.status, b.status].sort();
    expect(statuses).toEqual(["accepted", "duplicate"]);

    const idOf = (r: typeof a): string =>
      r.status === "accepted" || r.status === "duplicate" ? r.event_id : "";
    expect(idOf(a)).toBe(idOf(b));

    const live = await sql<{ n: number }>`
      select count(*)::int as n from entitlements
      where tenant_id = 'tnt_t' and listing_id = ${listing}
    `.execute(db);
    expect(live.rows[0]?.n).toBe(1);
  });
});
