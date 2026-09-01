/**
 * Exercises the operator session lifecycle against a real Postgres brought up
 * by the @osds/db migrations. `db` is the `osds_app` pool (RLS enforced);
 * `owner` seeds `operators` rows and reads `operator_sessions` back with RLS
 * bypassed. Skips cleanly when no database is reachable.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { createKysely, sql } from "@osds/db";
import { hash as hashPassword, InvalidPasswordHashError } from "../password.js";
import {
  authenticateOperator,
  createSession,
  resolveSession,
  revokeAllForOperator,
  revokeSession,
  type PersistDeps,
} from "./index.js";
import {
  adminUrl,
  createScratchDb,
  dropScratchDb,
  pgReachable,
  type ScratchDb,
} from "./scratch-db.js";

const available = await pgReachable();
if (!available) {
  console.warn(
    `[persist/session.test] Postgres not reachable at ${adminUrl()} - skipping`,
  );
}

const TENANT_HOST = "chicagoplumbers.example";
const CONSOLE_HOST = "console.osds.example";
const LIFETIME_MS = 14 * 24 * 60 * 60 * 1000;
const T0 = "2026-09-01T12:00:00.000Z";

// An ln=14 hash of "rehash-me-please" - below password.ts's current ln=16, so
// authenticateOperator should upgrade it in place on a successful login.
const OLD_HASH =
  "$scrypt$ln=14,r=8,p=1$QFSdGDAGixJVpsPwnMd2/Q$Px8eEVd46EsW6koSUeefBvjYceRIuTV5c3O4DtqIWpQ";

(available ? describe : describe.skip)("operator session lifecycle", () => {
  let scratch: ScratchDb;
  let owner: ReturnType<typeof createKysely>;
  let db: ReturnType<typeof createKysely>;

  let seq = 0;
  const newId = (): string => `t${(seq++).toString().padStart(24, "0")}`;
  let clock = new Date(T0);
  const deps: PersistDeps = { now: () => clock, newId };

  async function seedOperator(
    id: string,
    email: string,
    passwordHash: string,
  ): Promise<void> {
    await sql`
      insert into operators (id, email, password_hash)
      values (${id}, ${email}, ${passwordHash})
    `.execute(owner);
  }

  async function sessionRows(operatorId: string) {
    const res = await sql<{
      issued_for_host: string;
      expires_at: Date;
    }>`
      select issued_for_host, expires_at
      from operator_sessions where operator_id = ${operatorId}
      order by issued_for_host
    `.execute(owner);
    return res.rows;
  }

  async function passwordHashOf(operatorId: string): Promise<string> {
    const res = await sql<{ password_hash: string }>`
      select password_hash from operators where id = ${operatorId}
    `.execute(owner);
    return res.rows[0]!.password_hash;
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

  beforeEach(() => {
    clock = new Date(T0);
  });

  it("createSession -> resolveSession round-trips on the same host", async () => {
    await seedOperator("op_rt", "rt@example.test", await hashPassword("pw"));

    const { token, expiresAt } = await createSession(db, deps, "op_rt", TENANT_HOST);
    expect(token).toHaveLength(43); // 32 bytes, base64url, no padding
    expect(expiresAt.getTime() - clock.getTime()).toBe(LIFETIME_MS);

    expect(await resolveSession(db, deps, token, TENANT_HOST)).toEqual({
      operatorId: "op_rt",
      expiresAt: new Date(expiresAt.toISOString()),
    });
  });

  it("resolves to null for a wrong token, or the right token at the wrong host", async () => {
    await seedOperator("op_wh", "wh@example.test", await hashPassword("pw"));
    const { token } = await createSession(db, deps, "op_wh", TENANT_HOST);

    expect(await resolveSession(db, deps, "not-a-real-token", TENANT_HOST)).toBeNull();
    expect(await resolveSession(db, deps, token, CONSOLE_HOST)).toBeNull();
    // The Host header's case does not matter - the resolver lowercases.
    expect(await resolveSession(db, deps, token, "ChicagoPlumbers.Example")).not.toBeNull();
  });

  it("resolves to null once the row is past its absolute expiry", async () => {
    await seedOperator("op_exp", "exp@example.test", await hashPassword("pw"));
    const { token, expiresAt } = await createSession(db, deps, "op_exp", TENANT_HOST);

    clock = new Date(expiresAt.getTime() + 1000);
    expect(await resolveSession(db, deps, token, TENANT_HOST)).toBeNull();
  });

  it("keeps one operator's two hosts as independent rows", async () => {
    await seedOperator("op_two", "two@example.test", await hashPassword("pw"));
    const a = await createSession(db, deps, "op_two", TENANT_HOST);
    const b = await createSession(db, deps, "op_two", CONSOLE_HOST);

    expect(a.token).not.toBe(b.token);
    expect((await sessionRows("op_two")).map((r) => r.issued_for_host)).toEqual([
      TENANT_HOST,
      CONSOLE_HOST,
    ]);
    expect(await resolveSession(db, deps, a.token, TENANT_HOST)).not.toBeNull();
    expect(await resolveSession(db, deps, b.token, CONSOLE_HOST)).not.toBeNull();
  });

  it("createSession does not extend an existing session - it adds a row", async () => {
    await seedOperator("op_abs", "abs@example.test", await hashPassword("pw"));
    const first = await createSession(db, deps, "op_abs", TENANT_HOST);

    clock = new Date(clock.getTime() + 60_000);
    const second = await createSession(db, deps, "op_abs", TENANT_HOST);

    expect(second.expiresAt.getTime()).toBeGreaterThan(first.expiresAt.getTime());
    const rows = await sessionRows("op_abs");
    expect(rows).toHaveLength(2);
    // The first row's expiry is untouched.
    expect(
      rows.some((r) => r.expires_at.getTime() === first.expiresAt.getTime()),
    ).toBe(true);
  });

  it("revokeSession removes only the session for that token and host", async () => {
    await seedOperator("op_rev", "rev@example.test", await hashPassword("pw"));
    const tenant = await createSession(db, deps, "op_rev", TENANT_HOST);
    const con = await createSession(db, deps, "op_rev", CONSOLE_HOST);

    await revokeSession(db, tenant.token, TENANT_HOST);

    expect(await resolveSession(db, deps, tenant.token, TENANT_HOST)).toBeNull();
    expect(await resolveSession(db, deps, con.token, CONSOLE_HOST)).not.toBeNull();
    await revokeSession(db, tenant.token, TENANT_HOST); // idempotent
  });

  it("revokeAllForOperator clears every host, and spares other operators", async () => {
    await seedOperator("op_all", "all@example.test", await hashPassword("pw"));
    await seedOperator("op_keep", "keep@example.test", await hashPassword("pw"));
    await createSession(db, deps, "op_all", TENANT_HOST);
    await createSession(db, deps, "op_all", CONSOLE_HOST);
    const kept = await createSession(db, deps, "op_keep", TENANT_HOST);

    await revokeAllForOperator(db, "op_all");

    expect(await sessionRows("op_all")).toHaveLength(0);
    expect(await resolveSession(db, deps, kept.token, TENANT_HOST)).not.toBeNull();
  });

  describe("authenticateOperator", () => {
    it("returns a working session for the right email and password", async () => {
      await seedOperator("op_ok", "ok@example.test", await hashPassword("s3cret"));

      const session = await authenticateOperator(
        db,
        deps,
        "OK@Example.Test",
        "s3cret",
        TENANT_HOST,
      );
      expect(session).not.toBeNull();
      expect(await resolveSession(db, deps, session!.token, TENANT_HOST)).toEqual({
        operatorId: "op_ok",
        expiresAt: expect.any(Date),
      });
    });

    it("returns null for a wrong password, writing no session", async () => {
      await seedOperator("op_bad", "bad@example.test", await hashPassword("s3cret"));
      expect(
        await authenticateOperator(db, deps, "bad@example.test", "wrong", TENANT_HOST),
      ).toBeNull();
      expect(await sessionRows("op_bad")).toHaveLength(0);
    });

    it("returns null for an unknown email", async () => {
      expect(
        await authenticateOperator(db, deps, "nobody@example.test", "whatever", CONSOLE_HOST),
      ).toBeNull();
    });

    it("rehashes a stale password hash in place on a successful login", async () => {
      await seedOperator("op_rh", "rh@example.test", OLD_HASH);

      const session = await authenticateOperator(
        db,
        deps,
        "rh@example.test",
        "rehash-me-please",
        TENANT_HOST,
      );
      expect(session).not.toBeNull();

      const after = await passwordHashOf("op_rh");
      expect(after).not.toBe(OLD_HASH);
      expect(after.startsWith("$scrypt$ln=16,")).toBe(true);
    });

    it("propagates InvalidPasswordHashError when the stored hash is corrupt", async () => {
      await seedOperator("op_corrupt", "corrupt@example.test", "not-a-hash");
      await expect(
        authenticateOperator(db, deps, "corrupt@example.test", "s3cret", TENANT_HOST),
      ).rejects.toBeInstanceOf(InvalidPasswordHashError);
    });
  });
});
