/**
 * Exercises the operator session lifecycle against a real Postgres brought up
 * by the @osds/db migrations. `db` is the `osds_app` pool (RLS enforced);
 * `owner` seeds `operators` rows and reads `operator_sessions` back with RLS
 * bypassed. Skips cleanly when no database is reachable.
 */
import { createHash } from "node:crypto";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { createKysely, sql } from "@osds/db";
import { hash as hashPassword, InvalidPasswordHashError } from "../password.js";
import {
  authenticateOperator,
  createSession,
  isLoginThrottled,
  resolveSession,
  revokeAllForOperator,
  revokeSession,
  type LoginThrottled,
  type PersistDeps,
  type Session,
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

/** Narrow `authenticateOperator`'s widened result to a real {@link Session}. */
function expectSession(
  result: Session | LoginThrottled | null,
): asserts result is Session {
  if (result === null || isLoginThrottled(result)) {
    throw new Error(`expected a Session, got ${JSON.stringify(result)}`);
  }
}

/** SHA-256 (hex) of a lowercased address - the `operator_login_attempts` key. */
const emailKey = (email: string): string =>
  createHash("sha256").update(email.toLowerCase()).digest("hex");

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
      expectSession(session);
      expect(await resolveSession(db, deps, session.token, TENANT_HOST)).toEqual({
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
      expectSession(session);

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

    // --- login-attempt limit (#86) ------------------------------------------

    it("throttles once the window count exceeds the limit, before the lookup", async () => {
      await seedOperator("op_thr", "thr@example.test", await hashPassword("s3cret"));

      for (let i = 0; i < 5; i++) {
        expect(
          await authenticateOperator(db, deps, "thr@example.test", "wrong", TENANT_HOST),
        ).toBeNull();
      }
      // The 6th attempt is refused even though the password is correct - the
      // counter is checked first.
      const sixth = await authenticateOperator(
        db,
        deps,
        "thr@example.test",
        "s3cret",
        TENANT_HOST,
      );
      expect(isLoginThrottled(sixth)).toBe(true);
      expect((sixth as LoginThrottled).retryAfterSeconds).toBe(15 * 60);
      expect(await sessionRows("op_thr")).toHaveLength(0);
    });

    it("counts attempts per submitted address, not per resolved operator", async () => {
      // No operator row for this address; it must still throttle, proving the
      // limit does not wait on the lookup.
      for (let i = 0; i < 5; i++) {
        expect(
          await authenticateOperator(db, deps, "ghost@example.test", "x", CONSOLE_HOST),
        ).toBeNull();
      }
      expect(
        isLoginThrottled(
          await authenticateOperator(db, deps, "ghost@example.test", "x", CONSOLE_HOST),
        ),
      ).toBe(true);
    });

    it("a throttled attempt never reaches the stored hash", async () => {
      // A corrupt hash makes verifyPassword throw. The first 5 attempts reach
      // it; the 6th is refused by the counter first, so it resolves instead.
      await seedOperator("op_cor", "cor@example.test", "not-a-hash");
      for (let i = 0; i < 5; i++) {
        await expect(
          authenticateOperator(db, deps, "cor@example.test", "s3cret", TENANT_HOST),
        ).rejects.toBeInstanceOf(InvalidPasswordHashError);
      }
      expect(
        isLoginThrottled(
          await authenticateOperator(db, deps, "cor@example.test", "s3cret", TENANT_HOST),
        ),
      ).toBe(true);
    });

    it("a successful login clears the counter", async () => {
      await seedOperator("op_clr", "clr@example.test", await hashPassword("s3cret"));

      for (let i = 0; i < 4; i++) {
        expect(
          await authenticateOperator(db, deps, "clr@example.test", "wrong", TENANT_HOST),
        ).toBeNull();
      }
      expectSession(
        await authenticateOperator(db, deps, "clr@example.test", "s3cret", TENANT_HOST),
      );

      // Counter reset: five fresh failures are needed before the next throttle.
      for (let i = 0; i < 5; i++) {
        expect(
          await authenticateOperator(db, deps, "clr@example.test", "wrong", TENANT_HOST),
        ).toBeNull();
      }
      expect(
        isLoginThrottled(
          await authenticateOperator(db, deps, "clr@example.test", "wrong", TENANT_HOST),
        ),
      ).toBe(true);
    });
  });

  describe("operator_login_attempts RLS", () => {
    const W = "2026-09-01T12:00:00.000Z";

    async function seedAttempt(emailHashHex: string, failures: number): Promise<void> {
      await sql`
        insert into operator_login_attempts (email_hash, window_start, failures)
        values (decode(${emailHashHex}, 'hex'), ${W}, ${failures})
      `.execute(owner);
    }

    /** Run `fn` on the app pool with `app.login_attempt_hash` set (or unset). */
    function asApp<T>(
      attemptHashHex: string | null,
      fn: (trx: ReturnType<typeof createKysely>) => Promise<T>,
    ): Promise<T> {
      return db.transaction().execute(async (trx) => {
        await sql`set local role osds_app`.execute(trx);
        if (attemptHashHex !== null) {
          await sql`select set_config('app.login_attempt_hash', ${attemptHashHex}, true)`.execute(
            trx,
          );
        }
        return fn(trx);
      });
    }

    async function ownerFailures(emailHashHex: string): Promise<number[]> {
      const res = await sql<{ failures: number }>`
        select failures from operator_login_attempts
        where email_hash = decode(${emailHashHex}, 'hex')
      `.execute(owner);
      return res.rows.map((r) => r.failures);
    }

    it("reads only the row whose digest the caller presents", async () => {
      const a = emailKey("rls-a@example.test");
      const b = emailKey("rls-b@example.test");
      await seedAttempt(a, 3);
      await seedAttempt(b, 7);

      const rows = await asApp(a, (trx) =>
        sql<{ failures: number }>`select failures from operator_login_attempts`
          .execute(trx)
          .then((r) => r.rows),
      );
      expect(rows).toEqual([{ failures: 3 }]);
    });

    it("an unset GUC matches nothing, though the row exists", async () => {
      await seedAttempt(emailKey("rls-c@example.test"), 2);
      const rows = await asApp(null, (trx) =>
        sql`select 1 from operator_login_attempts`.execute(trx).then((r) => r.rows),
      );
      expect(rows).toHaveLength(0);
    });

    it("cannot delete a row under another digest", async () => {
      const a = emailKey("rls-del-a@example.test");
      const b = emailKey("rls-del-b@example.test");
      await seedAttempt(a, 1);
      await seedAttempt(b, 1);

      await asApp(a, (trx) =>
        sql`delete from operator_login_attempts`.execute(trx),
      );

      expect(await ownerFailures(a)).toEqual([]); // its own row: gone
      expect(await ownerFailures(b)).toEqual([1]); // the other: untouched
    });

    it("cannot insert a row under another digest (WITH CHECK)", async () => {
      const a = emailKey("rls-ins-a@example.test");
      const b = emailKey("rls-ins-b@example.test");
      await expect(
        asApp(b, (trx) =>
          sql`
            insert into operator_login_attempts (email_hash, window_start, failures)
            values (decode(${a}, 'hex'), ${W}, 1)
          `.execute(trx),
        ),
      ).rejects.toThrow(/row-level security/i);
    });

    it("the upsert-returning path works under a matching digest", async () => {
      const a = emailKey("rls-upsert@example.test");
      const bump = (): Promise<number> =>
        asApp(a, (trx) =>
          sql<{ failures: number }>`
            insert into operator_login_attempts (email_hash, window_start, failures)
            values (decode(${a}, 'hex'), ${W}, 1)
            on conflict (email_hash, window_start)
              do update set failures = operator_login_attempts.failures + 1
            returning failures
          `
            .execute(trx)
            .then((r) => r.rows[0]!.failures),
        );
      expect(await bump()).toBe(1);
      expect(await bump()).toBe(2);
    });
  });
});
