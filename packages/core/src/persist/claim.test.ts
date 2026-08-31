/**
 * Exercises persistClaimSubmit / persistClaimApprove against a real Postgres
 * brought up by the @osds/db migrations. Skips cleanly when no database is
 * reachable.
 *
 * Point it at a server with `OSDS_TEST_DATABASE_URL` or `DATABASE_URL_ADMIN`;
 * defaults to the docker-compose dev instance. A uniquely named scratch
 * database is created and dropped per run.
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
  type PersistDeps,
} from "./index.js";

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

const scratchName = `osds_claim_persist_test_${randomUUID().replace(/-/g, "")}`;
const scratchUrl = ((): string => {
  const u = new URL(ADMIN_URL);
  u.pathname = `/${scratchName}`;
  return u.toString();
})();

const available = await pgReachable();
if (!available) {
  console.warn(
    `[persist/claim.test] Postgres not reachable at ${ADMIN_URL} - skipping`,
  );
}

const ENABLED: readonly ClaimMethod[] = ["manual", "phone_otp", "domain_email"];

const consent = {
  marketing_email: {
    granted: true,
    at: "2026-08-28T14:22:10.000Z",
    ip: "203.0.113.44",
    text_version: "consent-v3",
  },
  automated_calls: {
    granted: false,
    at: null,
    ip: null,
    text_version: "consent-v3",
  },
};

function claimant(email: string, name = "Dana Hoffman") {
  return {
    name,
    email,
    phone_e164: "+17735550142",
    role_claimed: "owner",
  };
}

(available ? describe : describe.skip)(
  "claim persistence (real Postgres)",
  () => {
    let db: ReturnType<typeof createKysely>;

    let seq = 0;
    const newId = (): string =>
      `${Date.now().toString().padStart(15, "0")}${(seq++).toString().padStart(9, "0")}`;
    const FIXED_NOW = new Date("2026-08-31T12:00:00.000Z");
    const deps: PersistDeps = { now: () => FIXED_NOW, newId };

    function submitCmd(
      payload: Record<string, unknown>,
      over: Partial<OsdsCommand> = {},
    ): OsdsCommand {
      return {
        command: "claim.submit",
        idempotency_key: `k_${newId()}`,
        tenant_id: "tnt_a",
        adapter_id: "gohighlevel",
        trace_id: `tr_${newId()}`,
        payload,
        ...over,
      };
    }

    function approveCmd(
      payload: Record<string, unknown>,
      over: Partial<OsdsCommand> = {},
    ): OsdsCommand {
      return {
        command: "claim.approve",
        idempotency_key: `k_${newId()}`,
        tenant_id: "tnt_a",
        adapter_id: "admin-console",
        trace_id: `tr_${newId()}`,
        payload,
        ...over,
      };
    }

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

    async function seedListing(
      id: string,
      tenantId: string,
      slug: string,
      status = "unclaimed",
    ) {
      await sql`
      insert into listings (id, tenant_id, slug, name, status)
      values (${id}, ${tenantId}, ${slug}, ${slug}, ${status})
    `.execute(db);
    }

    async function seedUser(
      id: string,
      tenantId: string,
      email: string,
      name: string,
    ) {
      await sql`
      insert into users (id, tenant_id, email, name, role)
      values (${id}, ${tenantId}, ${email}, ${name}, 'owner')
    `.execute(db);
    }

    async function outboxByTrace(tenantId: string, traceId: string) {
      return asTenant(tenantId, async (trx) => {
        const res = await sql<{
          id: string;
          type: string;
          subject: string;
          origin: string;
          trace_id: string;
          idempotency_key: string | null;
          data: Record<string, unknown>;
        }>`
        select id, type, subject, origin, trace_id, idempotency_key, data
        from outbox where trace_id = ${traceId} order by id
      `.execute(trx);
        return [...res.rows];
      });
    }

    async function outboxBySubject(tenantId: string, subject: string) {
      return asTenant(tenantId, async (trx) => {
        const res = await sql<{
          id: string;
          type: string;
          idempotency_key: string | null;
        }>`
        select id, type, idempotency_key
        from outbox where subject = ${subject} order by id
      `.execute(trx);
        return [...res.rows];
      });
    }

    async function claimsForListing(tenantId: string, listingId: string) {
      return asTenant(tenantId, async (trx) => {
        const res = await sql<{
          id: string;
          status: string;
          method: string;
          claimant_user_id: string | null;
          decided_by: string | null;
          decided_at: Date | null;
          consent: unknown;
          verification: unknown;
        }>`
        select id, status, method, claimant_user_id, decided_by, decided_at,
               consent, verification
        from claims where listing_id = ${listingId} order by id
      `.execute(trx);
        return [...res.rows];
      });
    }

    async function usersByEmail(tenantId: string, email: string) {
      return asTenant(tenantId, async (trx) => {
        const res = await sql<{ id: string; name: string; role: string }>`
        select id, name, role from users where email = ${email} order by id
      `.execute(trx);
        return [...res.rows];
      });
    }

    async function listingRow(tenantId: string, id: string) {
      return asTenant(tenantId, async (trx) => {
        const res = await sql<{
          status: string;
          owner_user_id: string | null;
        }>`
        select status, owner_user_id from listings where id = ${id}
      `.execute(trx);
        return res.rows[0] ?? null;
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

    it("submit: mints a new claimant user and writes both events, key on the first only", async () => {
      await seedListing("listing_s1", "tnt_a", "hoffman-1");
      const cmd = submitCmd({
        listing_id: "listing_s1",
        method: "phone_otp",
        claimant: claimant("Dana@New.Example", "Dana"),
        consent,
      });

      const res = await persistClaimSubmit(db, cmd, deps, ENABLED);
      expect(res.status).toBe("submitted");
      const eventId = res.status === "submitted" ? res.event_id : "";

      const claims = await claimsForListing("tnt_a", "listing_s1");
      expect(claims).toHaveLength(1);
      expect(claims[0]!.status).toBe("verifying");
      expect(claims[0]!.method).toBe("phone_otp");
      const claimantUserId = claims[0]!.claimant_user_id!;
      expect(claimantUserId.startsWith("usr_")).toBe(true);
      expect(claims[0]!.consent).toEqual(consent);
      expect(claims[0]!.verification).toEqual({
        method: "phone_otp",
        expires_at: null,
      });

      const users = await usersByEmail("tnt_a", "dana@new.example");
      expect(users).toHaveLength(1);
      expect(users[0]!.id).toBe(claimantUserId);
      expect(users[0]!.role).toBe("owner");
      expect(users[0]!.name).toBe("Dana");

      const events = await outboxByTrace("tnt_a", cmd.trace_id);
      expect(events.map((e) => e.type)).toEqual([
        "claim.submitted",
        "claim.verification_started",
      ]);
      expect(events[0]!.id).toBe(eventId);
      expect(events[0]!.subject).toBe("listing_s1");
      expect(events[0]!.idempotency_key).toBe(cmd.idempotency_key);
      expect(events[1]!.idempotency_key).toBeNull();
      expect(events[0]!.origin).toBe("gohighlevel");
      expect(events[0]!.data).toMatchObject({
        claim: {
          id: claims[0]!.id,
          listing_id: "listing_s1",
          method: "phone_otp",
        },
        claimant: {
          id: claimantUserId,
          email: "dana@new.example",
          name: "Dana",
        },
        consent,
      });
      expect(events[1]!.data).toEqual({
        method: "phone_otp",
        expires_at: null,
      });
    });

    it("submit: reuses an existing user on the same (tenant, lowercased email)", async () => {
      await seedListing("listing_s2", "tnt_a", "hoffman-2");
      await seedUser("usr_existing_2", "tnt_a", "reuse@x.example", "Old Name");

      const cmd = submitCmd({
        listing_id: "listing_s2",
        method: "phone_otp",
        claimant: claimant("REUSE@X.Example", "New Name"),
        consent,
      });
      const res = await persistClaimSubmit(db, cmd, deps, ENABLED);
      expect(res.status).toBe("submitted");

      const users = await usersByEmail("tnt_a", "reuse@x.example");
      expect(users).toHaveLength(1);
      expect(users[0]!.id).toBe("usr_existing_2");
      // The no-op upsert must not clobber the existing profile.
      expect(users[0]!.name).toBe("Old Name");

      const claims = await claimsForListing("tnt_a", "listing_s2");
      expect(claims[0]!.claimant_user_id).toBe("usr_existing_2");

      const events = await outboxByTrace("tnt_a", cmd.trace_id);
      expect(events[0]!.data).toMatchObject({
        claimant: { id: "usr_existing_2" },
      });
    });

    it("disputed: a claim on an already-claimed listing writes only the event", async () => {
      await seedListing("listing_s3", "tnt_a", "hoffman-3", "claimed");
      const cmd = submitCmd({
        listing_id: "listing_s3",
        method: "phone_otp",
        claimant: claimant("disputer@x.example"),
        consent,
      });

      const res = await persistClaimSubmit(db, cmd, deps, ENABLED);
      expect(res.status).toBe("disputed");
      const eventId = res.status === "disputed" ? res.event_id : "";

      const events = await outboxByTrace("tnt_a", cmd.trace_id);
      expect(events).toHaveLength(1);
      expect(events[0]!.type).toBe("claim.disputed");
      expect(events[0]!.id).toBe(eventId);
      expect(events[0]!.subject).toBe("listing_s3");
      expect(events[0]!.idempotency_key).toBe(cmd.idempotency_key);

      // No state change: no claims row, no claimant user.
      expect(await claimsForListing("tnt_a", "listing_s3")).toHaveLength(0);
      expect(await usersByEmail("tnt_a", "disputer@x.example")).toHaveLength(0);

      const data = events[0]!.data as { claim: Record<string, unknown> };
      expect(data.claim).toEqual({
        listing_id: "listing_s3",
        status: "disputed",
        method: "phone_otp",
      });
    });

    it("approve: writes claim.approved then listing.owner_assigned, key on the first only", async () => {
      await seedListing("listing_s4", "tnt_a", "hoffman-4");
      await seedUser("usr_admin_4", "tnt_a", "admin4@x.example", "Admin Four");

      const submitRes = await persistClaimSubmit(
        db,
        submitCmd({
          listing_id: "listing_s4",
          method: "phone_otp",
          claimant: claimant("owner4@x.example"),
          consent,
        }),
        deps,
        ENABLED,
      );
      expect(submitRes.status).toBe("submitted");

      const claimBefore = (await claimsForListing("tnt_a", "listing_s4"))[0]!;
      const ownerUserId = claimBefore.claimant_user_id!;

      const cmd = approveCmd({
        claim_id: claimBefore.id,
        decided_by: "usr_admin_4",
      });
      const res = await persistClaimApprove(db, cmd, deps);
      expect(res.status).toBe("approved");
      const eventId = res.status === "approved" ? res.event_id : "";

      const events = await outboxByTrace("tnt_a", cmd.trace_id);
      expect(events.map((e) => e.type)).toEqual([
        "claim.approved",
        "listing.owner_assigned",
      ]);
      expect(events[0]!.id).toBe(eventId);
      expect(events[0]!.subject).toBe("listing_s4");
      expect(events[1]!.subject).toBe("listing_s4");
      expect(events[0]!.idempotency_key).toBe(cmd.idempotency_key);
      expect(events[1]!.idempotency_key).toBeNull();
      expect(events[1]!.data).toEqual({
        owner_user_id: ownerUserId,
        claim_id: claimBefore.id,
      });

      const claimAfter = (await claimsForListing("tnt_a", "listing_s4"))[0]!;
      expect(claimAfter.status).toBe("approved");
      expect(claimAfter.decided_by).toBe("usr_admin_4");
      expect(claimAfter.decided_at).not.toBeNull();

      // Ownership is projected onto the listing (issue #45).
      expect(await listingRow("tnt_a", "listing_s4")).toEqual({
        status: "claimed",
        owner_user_id: ownerUserId,
      });
    });

    it("replay: a two-event command re-runs to a single duplicate, writing nothing", async () => {
      await seedListing("listing_s5", "tnt_a", "hoffman-5");
      const cmd = submitCmd({
        listing_id: "listing_s5",
        method: "phone_otp",
        claimant: claimant("replay@x.example"),
        consent,
      });

      const first = await persistClaimSubmit(db, cmd, deps, ENABLED);
      const second = await persistClaimSubmit(db, cmd, deps, ENABLED);

      expect(first.status).toBe("submitted");
      const firstId = first.status === "submitted" ? first.event_id : "";
      expect(second).toEqual({ status: "duplicate", event_id: firstId });

      // The replay is only safe because the first transaction wrote BOTH events:
      // exactly two rows, the trailing one keyed null, still present.
      const events = await outboxByTrace("tnt_a", cmd.trace_id);
      expect(events).toHaveLength(2);
      expect(events.map((e) => e.type)).toEqual([
        "claim.submitted",
        "claim.verification_started",
      ]);
      expect(events[0]!.id).toBe(firstId);
      expect(events[0]!.idempotency_key).toBe(cmd.idempotency_key);
      expect(events[1]!.idempotency_key).toBeNull();

      // And no second claim / user from the replay.
      expect(await claimsForListing("tnt_a", "listing_s5")).toHaveLength(1);
      expect(await usersByEmail("tnt_a", "replay@x.example")).toHaveLength(1);
    });

    // Seed one unclaimed listing plus two submitted claims on it. Returns the
    // two claim ids in insertion order.
    async function twoClaimsOnListing(
      listingId: string,
      slug: string,
    ): Promise<[string, string]> {
      await seedListing(listingId, "tnt_a", slug);
      for (const who of ["first", "second"]) {
        await persistClaimSubmit(
          db,
          submitCmd({
            listing_id: listingId,
            method: "phone_otp",
            claimant: claimant(`${who}-${slug}@x.example`),
            consent,
          }),
          deps,
          ENABLED,
        );
      }
      const claims = await claimsForListing("tnt_a", listingId);
      expect(claims).toHaveLength(2);
      return [claims[0]!.id, claims[1]!.id];
    }

    it("approve: a second claim on a now-owned listing is rejected (§9.4), owner untouched", async () => {
      await seedUser("usr_admin_6", "tnt_a", "admin6@x.example", "Admin Six");
      const [c1, c2] = await twoClaimsOnListing("listing_s6", "hoffman-6");
      const sittingOwner = (await claimsForListing("tnt_a", "listing_s6")).find(
        (c) => c.id === c1,
      )!.claimant_user_id;

      const r1 = await persistClaimApprove(
        db,
        approveCmd({ claim_id: c1, decided_by: "usr_admin_6" }),
        deps,
      );
      const r2 = await persistClaimApprove(
        db,
        approveCmd({ claim_id: c2, decided_by: "usr_admin_6" }),
        deps,
      );

      expect(r1.status).toBe("approved");
      expect(r2.status).toBe("rejected");
      if (r2.status === "rejected") {
        expect(r2.problem.status).toBe(422);
        expect(String(r2.problem.detail)).toContain("§9.4");
      }

      const claims = await claimsForListing("tnt_a", "listing_s6");
      expect(
        claims.filter((c) => c.status === "approved").map((c) => c.id),
      ).toEqual([c1]);
      expect(claims.find((c) => c.id === c2)!.status).toBe("verifying");

      // The rejected approval leaves the sitting owner in place.
      expect(await listingRow("tnt_a", "listing_s6")).toEqual({
        status: "claimed",
        owner_user_id: sittingOwner,
      });

      const assigned = (await outboxBySubject("tnt_a", "listing_s6")).filter(
        (e) => e.type === "listing.owner_assigned",
      );
      expect(assigned).toHaveLength(1);
    });

    it("approve: two concurrent approvals of different claims on one listing - exactly one wins", async () => {
      await seedUser("usr_admin_7", "tnt_a", "admin7@x.example", "Admin Seven");
      const [c1, c2] = await twoClaimsOnListing("listing_s7", "hoffman-7");

      const [r1, r2] = await Promise.all([
        persistClaimApprove(
          db,
          approveCmd({ claim_id: c1, decided_by: "usr_admin_7" }),
          deps,
        ),
        persistClaimApprove(
          db,
          approveCmd({ claim_id: c2, decided_by: "usr_admin_7" }),
          deps,
        ),
      ]);

      expect([r1.status, r2.status].sort()).toEqual(["approved", "rejected"]);

      const claims = await claimsForListing("tnt_a", "listing_s7");
      const winner = claims.find((c) => c.status === "approved")!;
      expect(claims.filter((c) => c.status === "approved")).toHaveLength(1);

      // The listing's owner is exactly the one winning claim's claimant.
      expect(await listingRow("tnt_a", "listing_s7")).toEqual({
        status: "claimed",
        owner_user_id: winner.claimant_user_id,
      });

      const assigned = (await outboxBySubject("tnt_a", "listing_s7")).filter(
        (e) => e.type === "listing.owner_assigned",
      );
      expect(assigned).toHaveLength(1);
    });

    it("cross-tenant: the same slug and claimant email resolve independently per tenant", async () => {
      await seedListing("listing_xa", "tnt_a", "shared");
      await seedListing("listing_xb", "tnt_b", "shared");

      const aCmd = submitCmd({
        listing_id: "listing_xa",
        method: "phone_otp",
        claimant: claimant("cross@x.example", "A Person"),
        consent,
      });
      const bCmd = submitCmd(
        {
          listing_id: "listing_xb",
          method: "phone_otp",
          claimant: claimant("cross@x.example", "B Person"),
          consent,
        },
        { tenant_id: "tnt_b" },
      );
      const a = await persistClaimSubmit(db, aCmd, deps, ENABLED);
      const b = await persistClaimSubmit(db, bCmd, deps, ENABLED);
      expect(a.status).toBe("submitted");
      expect(b.status).toBe("submitted");

      const aClaims = await claimsForListing("tnt_a", "listing_xa");
      const bClaims = await claimsForListing("tnt_b", "listing_xb");
      expect(aClaims).toHaveLength(1);
      expect(bClaims).toHaveLength(1);
      expect(aClaims[0]!.id).not.toBe(bClaims[0]!.id);
      expect(aClaims[0]!.claimant_user_id).not.toBe(
        bClaims[0]!.claimant_user_id,
      );

      const aUsers = await usersByEmail("tnt_a", "cross@x.example");
      const bUsers = await usersByEmail("tnt_b", "cross@x.example");
      expect(aUsers).toHaveLength(1);
      expect(bUsers).toHaveLength(1);
      expect(aUsers[0]!.id).toBe(aClaims[0]!.claimant_user_id);
      expect(aUsers[0]!.name).toBe("A Person");
      expect(bUsers[0]!.name).toBe("B Person");

      // A's tenant-scoped session never sees B's claim rows or events; B's does.
      expect(await claimsForListing("tnt_a", "listing_xb")).toHaveLength(0);
      expect(await outboxByTrace("tnt_a", bCmd.trace_id)).toHaveLength(0);
      expect(await outboxByTrace("tnt_b", bCmd.trace_id)).toHaveLength(2);
    });
  },
);
