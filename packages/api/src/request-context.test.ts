/**
 * `resolveRequestContext` against a real Postgres brought up by the @osds/db
 * migrations. `db` is the `osds_app` pool (RLS enforced, exactly as the app
 * connects); `owner` is a superuser that seeds rows and bypasses RLS. Skips
 * cleanly when no database is reachable.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { createKysely, sql } from "@osds/db";
import { hashPassword, ulidFactory } from "@osds/core";
import {
  authenticateOperator,
  tokenHashOf,
  type PersistDeps,
} from "@osds/core/persist";
import { ROLE_RANK, normalizeHost, resolveRequestContext } from "./index.js";
import {
  adminUrl,
  createScratchDb,
  dropScratchDb,
  pgReachable,
  type ScratchDb,
} from "./scratch-db.js";

describe("spec §4.4 role rules, re-exported from @osds/api", () => {
  it("orders the five ranks admin > manager > editor > moderator > support", () => {
    expect(ROLE_RANK).toEqual({
      admin: 4,
      manager: 3,
      editor: 2,
      moderator: 1,
      support: 0,
    });
  });
});

describe("normalizeHost - the single host normalizer @osds/web also uses", () => {
  it.each<[string, string]>([
    ["Example.COM", "example.com"],
    ["example.com:3000", "example.com"],
    ["  Example.com  ", "example.com"],
    ["[::1]:8080", "[::1]"],
    ["[2001:db8::1]", "[2001:db8::1]"],
    ["", ""],
  ])("%j -> %j", (input, expected) => {
    expect(normalizeHost(input)).toBe(expected);
  });
});

const TENANT_HOST = "chicagoplumbers.example";
const CONSOLE_HOST = "console.osds.example";
const OTHER_TENANT_HOST = "denverroofers.example";

const available = await pgReachable();
if (!available) {
  console.warn(
    `[api/request-context.test] Postgres not reachable at ${adminUrl()} - skipping`,
  );
}

(available ? describe : describe.skip)("resolveRequestContext", () => {
  let scratch: ScratchDb;
  let owner: ReturnType<typeof createKysely>;
  let db: ReturnType<typeof createKysely>;

  let sesSeq = 0;

  async function seedTenant(id: string, slug: string, domain: string) {
    await sql`
      insert into tenants (id, slug, domain, name)
      values (${id}, ${slug}, ${domain}, ${slug})
    `.execute(owner);
  }

  async function seedOperator(
    id: string,
    email: string,
    opts: { superadmin?: boolean; passwordHash?: string } = {},
  ) {
    await sql`
      insert into operators (id, email, password_hash, is_superadmin)
      values (
        ${id}, ${email}, ${opts.passwordHash ?? "x"}, ${opts.superadmin ?? false}
      )
    `.execute(owner);
  }

  async function seedMembership(
    operatorId: string,
    tenantId: string,
    role: string,
    status: "pending" | "active",
  ) {
    await sql`
      insert into staff_memberships (operator_id, tenant_id, role, status)
      values (${operatorId}, ${tenantId}, ${role}, ${status})
    `.execute(owner);
  }

  /** Seed a session row and return the raw token the caller would present. */
  async function seedSession(
    operatorId: string,
    issuedForHost: string,
    opts: { expired?: boolean } = {},
  ): Promise<string> {
    const token = `tok-${(sesSeq++).toString().padStart(4, "0")}-secret`;
    const expiresAt = opts.expired
      ? sql`now() - interval '1 day'`
      : sql`now() + interval '14 days'`;
    await sql`
      insert into operator_sessions
        (id, operator_id, token_hash, issued_for_host, expires_at)
      values (
        ${`ses_${(sesSeq).toString().padStart(4, "0")}`}, ${operatorId},
        ${tokenHashOf(token)}, ${issuedForHost}, ${expiresAt}
      )
    `.execute(owner);
    return token;
  }

  beforeAll(async () => {
    scratch = await createScratchDb();
    owner = createKysely(scratch.ownerUrl);
    db = createKysely(scratch.appUrl);
  });

  afterAll(async () => {
    await db?.destroy();
    await owner?.destroy();
    if (scratch) await dropScratchDb(scratch.name);
  });

  beforeEach(async () => {
    // Fresh rows per test - simplest isolation, and fileParallelism is off.
    await sql`delete from operator_sessions`.execute(owner);
    await sql`delete from staff_memberships`.execute(owner);
    await sql`delete from operators`.execute(owner);
    await sql`delete from tenants`.execute(owner);
    await seedTenant("tnt_chi", "chicago", TENANT_HOST);
    await seedTenant("tnt_den", "denver", OTHER_TENANT_HOST);
  });

  const CONSOLE = { consoleHost: CONSOLE_HOST } as const;

  it("unknown host: no tenant match, not the console -> kind 'unknown', no session lookup", async () => {
    const ctx = await resolveRequestContext(
      { host: "nowhere.example", sessionToken: "anything", ...CONSOLE },
      db,
    );
    expect(ctx).toEqual({ kind: "unknown", host: "nowhere.example" });
  });

  it("tenant host, no cookie -> tenant context with operator null", async () => {
    const ctx = await resolveRequestContext(
      { host: TENANT_HOST, sessionToken: null, ...CONSOLE },
      db,
    );
    expect(ctx).toEqual({
      kind: "tenant",
      host: TENANT_HOST,
      tenantId: "tnt_chi",
      operator: null,
    });
  });

  it("tenant host, unrecognised token -> operator null", async () => {
    const ctx = await resolveRequestContext(
      { host: TENANT_HOST, sessionToken: "not-a-real-token", ...CONSOLE },
      db,
    );
    expect(ctx).toMatchObject({ kind: "tenant", operator: null });
  });

  it("tenant host, active editor session -> operator carries the role", async () => {
    await seedOperator("op_ed", "ed@example.test");
    await seedMembership("op_ed", "tnt_chi", "editor", "active");
    const token = await seedSession("op_ed", TENANT_HOST);

    const ctx = await resolveRequestContext(
      { host: TENANT_HOST, sessionToken: token, ...CONSOLE },
      db,
    );
    expect(ctx).toEqual({
      kind: "tenant",
      host: TENANT_HOST,
      tenantId: "tnt_chi",
      operator: {
        operatorId: "op_ed",
        email: "ed@example.test",
        isSuperadmin: false,
        role: "editor",
      },
    });
  });

  it("Host header case and port do not matter - the resolver normalizes", async () => {
    await seedOperator("op_cp", "cp@example.test");
    await seedMembership("op_cp", "tnt_chi", "manager", "active");
    const token = await seedSession("op_cp", TENANT_HOST);

    const ctx = await resolveRequestContext(
      { host: "ChicagoPlumbers.Example:443", sessionToken: token, ...CONSOLE },
      db,
    );
    expect(ctx).toMatchObject({
      kind: "tenant",
      host: TENANT_HOST,
      operator: { operatorId: "op_cp", role: "manager" },
    });
  });

  it("tenant host, session issued for a different host -> operator null (host binding)", async () => {
    await seedOperator("op_hb", "hb@example.test");
    await seedMembership("op_hb", "tnt_chi", "admin", "active");
    const token = await seedSession("op_hb", CONSOLE_HOST); // issued for the console

    const ctx = await resolveRequestContext(
      { host: TENANT_HOST, sessionToken: token, ...CONSOLE },
      db,
    );
    expect(ctx).toMatchObject({ kind: "tenant", operator: null });
  });

  it("tenant host, expired session -> operator null", async () => {
    await seedOperator("op_ex", "ex@example.test");
    await seedMembership("op_ex", "tnt_chi", "editor", "active");
    const token = await seedSession("op_ex", TENANT_HOST, { expired: true });

    const ctx = await resolveRequestContext(
      { host: TENANT_HOST, sessionToken: token, ...CONSOLE },
      db,
    );
    expect(ctx).toMatchObject({ kind: "tenant", operator: null });
  });

  it("tenant host, only a pending membership -> role null (confers nothing, §4.4)", async () => {
    await seedOperator("op_pd", "pd@example.test");
    await seedMembership("op_pd", "tnt_chi", "admin", "pending");
    const token = await seedSession("op_pd", TENANT_HOST);

    const ctx = await resolveRequestContext(
      { host: TENANT_HOST, sessionToken: token, ...CONSOLE },
      db,
    );
    expect(ctx).toMatchObject({
      kind: "tenant",
      operator: { operatorId: "op_pd", role: null },
    });
  });

  it("tenant host, no membership on this tenant -> role null", async () => {
    await seedOperator("op_nm", "nm@example.test");
    await seedMembership("op_nm", "tnt_den", "admin", "active"); // other tenant
    const token = await seedSession("op_nm", TENANT_HOST);

    const ctx = await resolveRequestContext(
      { host: TENANT_HOST, sessionToken: token, ...CONSOLE },
      db,
    );
    expect(ctx).toMatchObject({
      kind: "tenant",
      operator: { operatorId: "op_nm", role: null },
    });
  });

  it("tenant host, superadmin with no membership -> isSuperadmin true, role null", async () => {
    await seedOperator("op_sa", "sa@example.test", { superadmin: true });
    const token = await seedSession("op_sa", TENANT_HOST);

    const ctx = await resolveRequestContext(
      { host: TENANT_HOST, sessionToken: token, ...CONSOLE },
      db,
    );
    expect(ctx).toMatchObject({
      kind: "tenant",
      operator: { operatorId: "op_sa", isSuperadmin: true, role: null },
    });
  });

  it("console host, active session -> console context, operator without a role", async () => {
    await seedOperator("op_co", "co@example.test");
    await seedMembership("op_co", "tnt_chi", "editor", "active");
    const token = await seedSession("op_co", CONSOLE_HOST);

    const ctx = await resolveRequestContext(
      { host: CONSOLE_HOST, sessionToken: token, ...CONSOLE },
      db,
    );
    expect(ctx).toEqual({
      kind: "console",
      host: CONSOLE_HOST,
      operator: {
        operatorId: "op_co",
        email: "co@example.test",
        isSuperadmin: false,
      },
    });
    expect(ctx.kind === "console" && ctx.operator !== null && "role" in ctx.operator).toBe(false);
  });

  it("console host, session issued for a tenant host -> operator null (host binding)", async () => {
    await seedOperator("op_ct", "ct@example.test");
    const token = await seedSession("op_ct", TENANT_HOST);

    const ctx = await resolveRequestContext(
      { host: CONSOLE_HOST, sessionToken: token, ...CONSOLE },
      db,
    );
    expect(ctx).toEqual({
      kind: "console",
      host: CONSOLE_HOST,
      operator: null,
    });
  });

  it("console host takes precedence over a tenant domain that equals it", async () => {
    await seedTenant("tnt_clash", "clash", CONSOLE_HOST);
    const ctx = await resolveRequestContext(
      { host: CONSOLE_HOST, sessionToken: null, ...CONSOLE },
      db,
    );
    expect(ctx).toEqual({ kind: "console", host: CONSOLE_HOST, operator: null });
  });

  it("a real authenticateOperator token resolves through resolveRequestContext at a ported host", async () => {
    // Closes the seam between createSession's `host.toLowerCase()` (which keeps
    // a port) and resolveRequestContext's `normalizeHost()` (which strips one).
    // authenticateOperator's DB behaviour and resolveRequestContext's are each
    // covered elsewhere, but never chained: a login token has not been proven
    // to resolve.
    const deps: PersistDeps = { now: () => new Date(), newId: ulidFactory };
    await seedOperator("op_rt", "rt@example.test", {
      passwordHash: await hashPassword("correct-horse-battery-staple"),
    });
    await seedMembership("op_rt", "tnt_chi", "editor", "active");

    // The browser reaches /admin at a host that carries a port and mixed case.
    // login/route.ts resolves the context first and logs in with `ctx.host` -
    // the normalized value resolveRequestContext will later match
    // `issued_for_host` against.
    const rawHost = "ChicagoPlumbers.Example:8443";
    const loginHost = normalizeHost(rawHost);

    const session = await authenticateOperator(
      db,
      deps,
      "rt@example.test",
      "correct-horse-battery-staple",
      loginHost,
    );
    expect(session).not.toBeNull();

    // A later request carries the raw, un-normalized Host header.
    const ctx = await resolveRequestContext(
      { host: rawHost, sessionToken: session!.token, ...CONSOLE },
      db,
    );
    expect(ctx).toEqual({
      kind: "tenant",
      host: TENANT_HOST,
      tenantId: "tnt_chi",
      operator: {
        operatorId: "op_rt",
        email: "rt@example.test",
        isSuperadmin: false,
        role: "editor",
      },
    });
  });
});
