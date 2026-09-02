/**
 * `dispatchCommand` - the gate matrix without a database, and the two
 * dispatchable commands against a real Postgres brought up by the @osds/db
 * migrations. `db` is the `osds_app` pool (RLS enforced). Skips the db-backed
 * block cleanly when no database is reachable.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { createKysely, sql } from "@osds/db";
import type { OsdsCommand } from "@osds/adapter-kit";
import {
  persistClaimSubmit,
  persistListingUpsert,
  type CommandActor,
  type PersistDeps,
} from "@osds/core/persist";
import {
  COMMAND_MIN_RANK,
  dispatchCommand,
  type RequestContext,
  type TenantContext,
} from "./index.js";
import {
  adminUrl,
  createScratchDb,
  dropScratchDb,
  pgReachable,
  type ScratchDb,
} from "./scratch-db.js";

let seq = 0;
const deps: PersistDeps = {
  now: () => new Date("2026-09-02T12:00:00.000Z"),
  newId: () => `t${(seq++).toString().padStart(24, "0")}`,
};

const TENANT_ID = "tnt_disp";
const TENANT_OTHER = "tnt_other";

function tenantCtx(over: Partial<TenantContext> = {}): TenantContext {
  return {
    kind: "tenant",
    host: "disp.example",
    tenantId: TENANT_ID,
    operator: {
      operatorId: "op_1",
      email: "op@example.test",
      isSuperadmin: false,
      role: "editor",
    },
    ...over,
  };
}

function body(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    command: "listing.upsert",
    idempotency_key: `k_${seq++}`,
    tenant_id: TENANT_ID,
    payload: { slug: "acme", name: "Acme" },
    ...over,
  };
}

// A db handle is only reached past every gate; the gate tests never touch it.
const noDb = null as never;

describe("dispatchCommand - COMMAND_MIN_RANK", () => {
  it("is editor (rank 2) for the two dispatchable commands", () => {
    expect(COMMAND_MIN_RANK).toEqual({
      "listing.upsert": 2,
      "claim.approve": 2,
    });
  });
});

describe("dispatchCommand - gates (no database)", () => {
  it("gate 1: a console surface is forbidden", async () => {
    const ctx: RequestContext = {
      kind: "console",
      host: "console.example",
      operator: null,
    };
    const out = await dispatchCommand(ctx, body(), noDb, deps);
    expect(out).toMatchObject({ kind: "forbidden" });
    expect(out.kind === "forbidden" && out.problem.status).toBe(403);
  });

  it("gate 1: an unknown surface is forbidden", async () => {
    const ctx: RequestContext = { kind: "unknown", host: "nope.example" };
    expect(await dispatchCommand(ctx, body(), noDb, deps)).toMatchObject({
      kind: "forbidden",
    });
  });

  it("gate 2: a tenant surface with no operator is unauthorized", async () => {
    const out = await dispatchCommand(
      tenantCtx({ operator: null }),
      body(),
      noDb,
      deps,
    );
    expect(out).toEqual({ kind: "unauthorized" });
  });

  it("envelope: a non-object body is rejected (422)", async () => {
    const out = await dispatchCommand(tenantCtx(), "not json", noDb, deps);
    expect(out).toMatchObject({ kind: "rejected" });
    expect(out.kind === "rejected" && out.problem.status).toBe(422);
  });

  it("envelope: a missing idempotency_key is rejected and named", async () => {
    const out = await dispatchCommand(
      tenantCtx(),
      body({ idempotency_key: "" }),
      noDb,
      deps,
    );
    expect(out.kind).toBe("rejected");
    if (out.kind === "rejected") {
      expect(out.problem.errors).toContain(
        "idempotency_key is required - mint one per submission",
      );
    }
  });

  it("routing: a real but not-yet-supported command is unsupported (422)", async () => {
    for (const command of [
      "claim.submit",
      "entitlement.grant",
      "listing.merge",
    ]) {
      const out = await dispatchCommand(
        tenantCtx(),
        body({ command }),
        noDb,
        deps,
      );
      expect(out).toMatchObject({ kind: "unsupported" });
      expect(out.kind === "unsupported" && out.problem.status).toBe(422);
    }
  });

  it("routing: entitlement.reportPayment is refused (403), not merely unsupported", async () => {
    const out = await dispatchCommand(
      tenantCtx(),
      body({ command: "entitlement.reportPayment" }),
      noDb,
      deps,
    );
    expect(out).toMatchObject({ kind: "forbidden" });
    expect(out.kind === "forbidden" && out.problem.code).toBe(
      "command_refused",
    );
  });

  it("gate 3: an envelope naming another tenant is forbidden", async () => {
    const out = await dispatchCommand(
      tenantCtx(),
      body({ tenant_id: "tnt_other" }),
      noDb,
      deps,
    );
    expect(out).toMatchObject({ kind: "forbidden" });
    expect(out.kind === "forbidden" && out.problem.code).toBe(
      "tenant_mismatch",
    );
  });

  it("gate 4: a role below the command's minimum is forbidden", async () => {
    const out = await dispatchCommand(
      tenantCtx({
        operator: {
          operatorId: "op_1",
          email: "op@example.test",
          isSuperadmin: false,
          role: "moderator", // rank 1 < editor 2
        },
      }),
      body(),
      noDb,
      deps,
    );
    expect(out).toMatchObject({ kind: "forbidden" });
    expect(out.kind === "forbidden" && out.problem.code).toBe(
      "role_insufficient",
    );
  });

  it("gate 4: a null role is forbidden, and a superadmin does not bypass it", async () => {
    const out = await dispatchCommand(
      tenantCtx({
        operator: {
          operatorId: "op_su",
          email: "su@example.test",
          isSuperadmin: true,
          role: null,
        },
      }),
      body(),
      noDb,
      deps,
    );
    expect(out).toMatchObject({ kind: "forbidden" });
    expect(out.kind === "forbidden" && out.problem.code).toBe(
      "role_insufficient",
    );
  });
});

const available = await pgReachable();
if (!available) {
  console.warn(
    `[api/dispatch.test] Postgres not reachable at ${adminUrl()} - skipping db-backed dispatch`,
  );
}

(available ? describe : describe.skip)(
  "dispatchCommand - real dispatch",
  () => {
    let scratch: ScratchDb;
    let owner: ReturnType<typeof createKysely>;
    let db: ReturnType<typeof createKysely>;

    const ADAPTER: CommandActor = { kind: "adapter", adapterId: "gohighlevel" };
    const CONSENT = {
      contact_by_business: {
        granted: true,
        at: "2026-08-28T14:22:10.000Z",
        ip: "203.0.113.44",
        text_version: "consent-v3",
      },
    };

    beforeAll(async () => {
      scratch = await createScratchDb();
      owner = createKysely(scratch.ownerUrl);
      db = createKysely(scratch.appUrl);
      await sql`
        insert into tenants (id, slug, name) values
          (${TENANT_ID}, 'disp', 'Disp'),
          (${TENANT_OTHER}, 'other', 'Other')
      `.execute(owner);
      await sql`
      insert into categories (id, tenant_id, slug, name)
      values ('cat_disp_plumbers', ${TENANT_ID}, 'plumbers', 'Plumbers')
    `.execute(owner);
    });

    afterAll(async () => {
      await db?.destroy();
      await owner?.destroy();
      if (scratch) await dropScratchDb(scratch.name);
    });

    beforeEach(async () => {
      await sql`delete from outbox`.execute(owner);
      await sql`delete from command_log`.execute(owner);
      await sql`delete from claims`.execute(owner);
      await sql`delete from listings`.execute(owner);
      await sql`delete from users`.execute(owner);
    });

    it("listing.upsert: dispatches, returns 202-shaped accepted with the event id", async () => {
      const cmd = body({
        payload: { slug: "hoffman", name: "Hoffman Plumbing" },
      });
      const out = await dispatchCommand(tenantCtx(), cmd, db, deps);

      expect(out.kind).toBe("accepted");
      const eventId = out.kind === "accepted" ? out.eventId : null;
      expect(eventId).toMatch(/^t0+\d+$/);

      // The event carries the operator actor, not an adapter (#95).
      const ev = await sql<{ actor: unknown; origin: string | null }>`
      select actor, origin from outbox where id = ${eventId}
    `.execute(owner);
      expect(ev.rows[0]!.actor).toEqual({ type: "staff", id: "op_1" });
      expect(ev.rows[0]!.origin).toBeNull();

      // No adapter was involved, so command_log.adapter_id is null (spec §7.1).
      const log = await sql<{
        adapter_id: string | null;
        outcome: string | null;
      }>`
      select adapter_id, outcome from command_log where idempotency_key = ${cmd["idempotency_key"]}
    `.execute(owner);
      expect(log.rows[0]).toEqual({ adapter_id: null, outcome: "created" });
    });

    it("listing.upsert: a replayed idempotency_key is duplicate (409-shaped), same event id", async () => {
      const cmd = body({ payload: { slug: "dup", name: "Dup" } });
      const first = await dispatchCommand(tenantCtx(), cmd, db, deps);
      const second = await dispatchCommand(tenantCtx(), cmd, db, deps);

      expect(first.kind).toBe("accepted");
      expect(second.kind).toBe("duplicate");
      const a = first.kind === "accepted" ? first.eventId : "a";
      const b = second.kind === "duplicate" ? second.eventId : "b";
      expect(b).toBe(a);
    });

    it("listing.upsert: a payload carrying tier is rejected (422) by the core resolver", async () => {
      const out = await dispatchCommand(
        tenantCtx(),
        body({ payload: { slug: "x", name: "X", tier: "featured" } }),
        db,
        deps,
      );
      expect(out.kind).toBe("rejected");
      expect(out.kind === "rejected" && out.problem.status).toBe(422);
    });

    it("gate 3 is load-bearing: a persist call with a foreign tenant_id writes to that tenant - RLS does not stop this, the gate does", async () => {
      // `dispatchCommand`'s gate 3 (envelope tenant == host tenant) would 403
      // this. Bypass it and go straight to `@osds/core/persist` to show what the
      // gate is the only thing preventing: `withTenant` sets `app.tenant_id`
      // from `command.tenant_id`, so RLS scopes the write to the *named* tenant,
      // not the operator's context tenant.
      const foreign: OsdsCommand = {
        command: "listing.upsert",
        idempotency_key: `k_foreign_${seq++}`,
        tenant_id: TENANT_OTHER, // an operator on TENANT_ID must never reach here
        adapter_id: null,
        trace_id: `tr_${seq++}`,
        payload: { slug: "leaked", name: "Leaked" },
      };
      const operatorActor: CommandActor = {
        kind: "operator",
        operatorId: "op_1",
        role: "editor",
      };

      const res = await persistListingUpsert(db, foreign, deps, operatorActor);
      expect(res.status).toBe("created"); // not rejected, not blocked by RLS

      const inOther = await sql<{ n: number }>`
        select count(*)::int as n from listings
        where tenant_id = ${TENANT_OTHER} and slug = 'leaked'
      `.execute(owner);
      const inContext = await sql<{ n: number }>`
        select count(*)::int as n from listings
        where tenant_id = ${TENANT_ID} and slug = 'leaked'
      `.execute(owner);
      expect(inOther.rows[0]!.n).toBe(1);
      expect(inContext.rows[0]!.n).toBe(0);
    });

    it("claim.approve: dispatches an approval and returns the event id", async () => {
      await sql`
      insert into listings (id, tenant_id, slug, name, status)
      values ('listing_ca', ${TENANT_ID}, 'ca', 'CA', 'unclaimed')
    `.execute(owner);
      await sql`
      insert into users (id, tenant_id, email, name, role)
      values ('usr_admin', ${TENANT_ID}, 'admin@x.example', 'Admin', 'owner')
    `.execute(owner);

      const submitCmd: OsdsCommand = {
        command: "claim.submit",
        idempotency_key: `k_sub_${seq++}`,
        tenant_id: TENANT_ID,
        adapter_id: "gohighlevel",
        trace_id: `tr_${seq++}`,
        payload: {
          listing_id: "listing_ca",
          method: "phone_otp",
          claimant: {
            name: "Dana Hoffman",
            email: "dana@x.example",
            phone_e164: "+17735550142",
            role_claimed: "owner",
          },
          consent: CONSENT,
        },
      };
      const sub = await persistClaimSubmit(db, submitCmd, deps, ADAPTER, [
        "manual",
        "phone_otp",
        "domain_email",
      ]);
      expect(sub.status).toBe("submitted");

      const claimId = (
        await sql<{
          id: string;
        }>`select id from claims where listing_id = 'listing_ca'`.execute(owner)
      ).rows[0]!.id;

      const out = await dispatchCommand(
        tenantCtx(),
        body({
          command: "claim.approve",
          payload: { claim_id: claimId, decided_by: "usr_admin" },
        }),
        db,
        deps,
      );
      expect(out.kind).toBe("accepted");
      expect(out.kind === "accepted" && out.eventId).toBeTruthy();
    });
  },
);
