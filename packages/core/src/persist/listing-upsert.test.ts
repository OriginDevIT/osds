/**
 * Exercises persistListingUpsert against a real Postgres brought up by the
 * @osds/db migrations. Skips cleanly when no database is reachable.
 *
 * Point it at a server with `OSDS_TEST_DATABASE_URL` or `DATABASE_URL_ADMIN`;
 * defaults to the docker-compose dev instance. A uniquely named scratch
 * database is created and dropped per run.
 *
 * The persist layer connects as `osds_app` (NOBYPASSRLS, not the table owner)
 * and sets the tenant GUC inside its own transaction, so the cross-tenant test
 * reads back through RLS the same way - a tenant-scoped session sees its own
 * rows and not the other tenant's.
 */
import { randomUUID } from "node:crypto";
import { Client } from "pg";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createKysely, migrateToLatest, sql } from "@osds/db";
import type { OsdsCommand } from "@osds/adapter-kit";
import type { JsonPatchOp } from "../command/json-patch.js";
import { persistListingUpsert, type PersistDeps } from "./index.js";
import { applyListingUpdate } from "./listing-upsert.js";

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

const scratchName = `osds_persist_test_${randomUUID().replace(/-/g, "")}`;
const scratchUrl = ((): string => {
  const u = new URL(ADMIN_URL);
  u.pathname = `/${scratchName}`;
  return u.toString();
})();

const available = await pgReachable();
if (!available) {
  console.warn(
    `[persist/listing-upsert.test] Postgres not reachable at ${ADMIN_URL} - skipping`,
  );
}

(available ? describe : describe.skip)(
  "persistListingUpsert (real Postgres)",
  () => {
    let db: ReturnType<typeof createKysely>;

    // Monotonic ids so `order by id` reflects insert order, like production ULIDs.
    let seq = 0;
    const newId = (): string =>
      `${Date.now().toString().padStart(15, "0")}${(seq++).toString().padStart(9, "0")}`;
    const FIXED_NOW = new Date("2026-08-31T12:00:00.000Z");
    const deps: PersistDeps = { now: () => FIXED_NOW, newId };

    function command(
      payload: Record<string, unknown>,
      over: Partial<OsdsCommand> = {},
    ): OsdsCommand {
      return {
        command: "listing.upsert",
        idempotency_key: `k_${newId()}`,
        tenant_id: "tnt_a",
        adapter_id: "webhook",
        trace_id: `tr_${newId()}`,
        payload,
        ...over,
      };
    }

    /** Run a read as `osds_app` scoped to one tenant - RLS decides what is visible. */
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

    async function listingRows(tenantId: string) {
      return asTenant(tenantId, async (trx) => {
        const res = await sql<{
          id: string;
          slug: string;
          name: string;
          updated_at: Date;
        }>`select id, slug, name, updated_at from listings order by id`.execute(
          trx,
        );
        return [...res.rows];
      });
    }

    async function outboxRows(tenantId: string, subject: string) {
      return asTenant(tenantId, async (trx) => {
        const res = await sql<{
          id: string;
          type: string;
          subject: string;
          origin: string;
          trace_id: string;
          idempotency_key: string | null;
          actor: unknown;
          data: unknown;
        }>`
        select id, type, subject, origin, trace_id, idempotency_key, actor, data
        from outbox where subject = ${subject} order by id
      `.execute(trx);
        return [...res.rows];
      });
    }

    beforeAll(async () => {
      const admin = new Client({ connectionString: ADMIN_URL });
      await admin.connect();
      await admin.query(`create database ${scratchName}`);
      await admin.end();

      const { error } = await migrateToLatest(scratchUrl);
      if (error) throw error;

      db = createKysely(scratchUrl);
      await sql`
      insert into tenants (id, slug, name) values
        ('tnt_a', 'tenant-a', 'Tenant A'),
        ('tnt_b', 'tenant-b', 'Tenant B')
    `.execute(db);
    });

    afterAll(async () => {
      await db?.destroy();
      const admin = new Client({ connectionString: ADMIN_URL });
      await admin.connect();
      await admin.query(`drop database if exists ${scratchName} with (force)`);
      await admin.end();
    });

    it("create: writes the listing row and one listing.created outbox row together", async () => {
      const cmd = command({
        slug: "hoffman-plumbing",
        name: "Hoffman Plumbing",
        contact: { email: "OFFICE@hoffman.example" },
      });

      const res = await persistListingUpsert(db, cmd, deps);

      expect(res.status).toBe("created");
      const eventId = res.status === "created" ? res.event_id : "";

      const rows = (await listingRows("tnt_a")).filter(
        (r) => r.slug === "hoffman-plumbing",
      );
      expect(rows).toHaveLength(1);
      const listingId = rows[0]!.id;
      expect(listingId.startsWith("listing_")).toBe(true);
      expect(rows[0]!.name).toBe("Hoffman Plumbing");

      const events = await outboxRows("tnt_a", listingId);
      expect(events).toHaveLength(1);
      expect(events[0]!.id).toBe(eventId);
      expect(events[0]!.type).toBe("listing.created");
      expect(events[0]!.subject).toBe(listingId);
      expect(events[0]!.origin).toBe("webhook");
      expect(events[0]!.trace_id).toBe(cmd.trace_id);
      expect(events[0]!.idempotency_key).toBe(cmd.idempotency_key);
      expect(events[0]!.actor).toEqual({ type: "adapter", id: "webhook" });
      expect(events[0]!.data).toMatchObject({
        listing: {
          id: listingId,
          tenant_id: "tnt_a",
          slug: "hoffman-plumbing",
          name: "Hoffman Plumbing",
          contact: {
            email: "office@hoffman.example",
            phone_e164: null,
            website: null,
          },
        },
      });
    });

    it("update: applies the changed column and writes a listing.updated JSON Patch", async () => {
      const created = await persistListingUpsert(
        db,
        command({ slug: "lakeview-hvac", name: "Lakeview HVAC" }),
        deps,
      );
      expect(created.status).toBe("created");

      const res = await persistListingUpsert(
        db,
        command({ slug: "lakeview-hvac", name: "Lakeview Heating & Cooling" }),
        deps,
      );

      expect(res.status).toBe("updated");

      const row = (await listingRows("tnt_a")).find(
        (r) => r.slug === "lakeview-hvac",
      )!;
      expect(row.name).toBe("Lakeview Heating & Cooling");

      const events = await outboxRows("tnt_a", row.id);
      expect(events.map((e) => e.type)).toEqual([
        "listing.created",
        "listing.updated",
      ]);
      expect(events[1]!.data).toEqual({
        changes: [
          { op: "replace", path: "/name", value: "Lakeview Heating & Cooling" },
        ],
      });
    });

    it("unchanged: writes nothing - no row update, no outbox row", async () => {
      await persistListingUpsert(
        db,
        command({ slug: "static-co", name: "Static Co" }),
        deps,
      );
      const before = (await listingRows("tnt_a")).find(
        (r) => r.slug === "static-co",
      )!;

      const res = await persistListingUpsert(
        db,
        command({ slug: "static-co", name: "Static Co" }),
        deps,
      );

      expect(res).toEqual({ status: "unchanged" });

      const after = (await listingRows("tnt_a")).find(
        (r) => r.slug === "static-co",
      )!;
      expect(after.updated_at.getTime()).toBe(before.updated_at.getTime());

      const events = await outboxRows("tnt_a", before.id);
      expect(events.map((e) => e.type)).toEqual(["listing.created"]);
    });

    it("rejected: a payload carrying tier writes nothing and returns the problem", async () => {
      const cmd = command({
        slug: "no-such-listing",
        name: "X",
        tier: "featured",
      });

      const res = await persistListingUpsert(db, cmd, deps);

      expect(res.status).toBe("rejected");
      if (res.status === "rejected") {
        expect(res.problem.status).toBe(422);
      }

      const rows = (await listingRows("tnt_a")).filter(
        (r) => r.slug === "no-such-listing",
      );
      expect(rows).toHaveLength(0);
      const orphan = await asTenant("tnt_a", async (trx) => {
        const r = await sql<{ n: number }>`
        select count(*)::int as n from outbox where idempotency_key = ${cmd.idempotency_key}
      `.execute(trx);
        return r.rows[0]!.n;
      });
      expect(orphan).toBe(0);
    });

    it("rejected: a payload carrying categories writes nothing (issue #42)", async () => {
      const cmd = command({
        slug: "cats-co",
        name: "Cats Co",
        categories: ["plumbers"],
      });

      const res = await persistListingUpsert(db, cmd, deps);

      expect(res.status).toBe("rejected");
      if (res.status === "rejected") {
        expect(res.problem.status).toBe(422);
        expect(JSON.stringify(res.problem.errors)).toContain("issue #42");
      }

      const rows = (await listingRows("tnt_a")).filter(
        (r) => r.slug === "cats-co",
      );
      expect(rows).toHaveLength(0);
      const orphan = await asTenant("tnt_a", async (trx) => {
        const r = await sql<{ n: number }>`
        select count(*)::int as n from outbox where idempotency_key = ${cmd.idempotency_key}
      `.execute(trx);
        return r.rows[0]!.n;
      });
      expect(orphan).toBe(0);
    });

    it("idempotent replay: the second call returns the original event id and re-emits nothing", async () => {
      const cmd = command({ slug: "replayed-co", name: "Replayed Co" });

      const first = await persistListingUpsert(db, cmd, deps);
      const second = await persistListingUpsert(db, cmd, deps);

      expect(first.status).toBe("created");
      const eventId = first.status === "created" ? first.event_id : "";
      expect(second).toEqual({ status: "duplicate", event_id: eventId });

      const rows = (await listingRows("tnt_a")).filter(
        (r) => r.slug === "replayed-co",
      );
      expect(rows).toHaveLength(1);

      const keyed = await asTenant("tnt_a", async (trx) => {
        const r = await sql<{ n: number }>`
        select count(*)::int as n from outbox where idempotency_key = ${cmd.idempotency_key}
      `.execute(trx);
        return r.rows[0]!.n;
      });
      expect(keyed).toBe(1);
    });

    it("cross-tenant: same slug in two tenants, each scoped session sees only its own", async () => {
      const a = await persistListingUpsert(
        db,
        command(
          { slug: "shared-slug", name: "A Company" },
          { tenant_id: "tnt_a" },
        ),
        deps,
      );
      const b = await persistListingUpsert(
        db,
        command(
          { slug: "shared-slug", name: "B Company" },
          { tenant_id: "tnt_b" },
        ),
        deps,
      );
      expect(a.status).toBe("created");
      expect(b.status).toBe("created");

      const aRows = (await listingRows("tnt_a")).filter(
        (r) => r.slug === "shared-slug",
      );
      const bRows = (await listingRows("tnt_b")).filter(
        (r) => r.slug === "shared-slug",
      );

      expect(aRows).toHaveLength(1);
      expect(bRows).toHaveLength(1);
      expect(aRows[0]!.name).toBe("A Company");
      expect(bRows[0]!.name).toBe("B Company");

      const aId = aRows[0]!.id;
      const bId = bRows[0]!.id;
      expect(aId).not.toBe(bId);

      // The scoped session for A does not see B's row at all (and vice versa) -
      // proven positively: each side returns its own id, never the other's.
      const allA = (await listingRows("tnt_a")).map((r) => r.id);
      const allB = (await listingRows("tnt_b")).map((r) => r.id);
      expect(allA).toContain(aId);
      expect(allA).not.toContain(bId);
      expect(allB).toContain(bId);
      expect(allB).not.toContain(aId);

      // B's create left A's outbox untouched.
      expect(await outboxRows("tnt_a", bId)).toHaveLength(0);
      expect(await outboxRows("tnt_b", bId)).toHaveLength(1);
    });

    // applyListingUpdate must never silently drop an op: a phantom listing.updated
    // event (assertion with no backing row change) is a design-rule-2 break. These
    // paths are unreachable given handleListingUpsert's current output; the guards
    // exist so a future resolver change fails loudly instead.
    describe("applyListingUpdate guards", () => {
      const run = (changes: JsonPatchOp[]) =>
        asTenant("tnt_a", (trx) =>
          applyListingUpdate(trx, "tnt_a", "listing_guard", changes),
        );

      it("throws on a patch path that maps to no listings column", async () => {
        await expect(
          run([{ op: "replace", path: "/categories", value: ["plumbers"] }]),
        ).rejects.toThrow(/maps to no listings column/);
      });

      it("throws on an op that is not a replace", async () => {
        await expect(run([{ op: "remove", path: "/name" }])).rejects.toThrow(
          /unexpected JSON Patch op "remove" at "\/name"/,
        );
      });

      it("throws when the change set produces no column assignments", async () => {
        await expect(run([])).rejects.toThrow(/no column assignments/);
      });
    });
  },
);
