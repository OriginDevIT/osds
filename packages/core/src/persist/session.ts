/**
 * Operator session lifecycle - decisions.md "Authentication" / "Admin
 * surfaces", migration 0017, issues #76 and #80.
 *
 * Persist functions only: mint / resolve / revoke a session row, plus the
 * login primitive that ties password verification to session creation. No
 * cookie handling, no `__Host-` logic, no HTTP - that is the API layer.
 *
 * Every function runs in one transaction as `osds_app` via {@link withAppRole}
 * (SET LOCAL ROLE, like `withTenant`, but no `app.tenant_id` - operator auth
 * and the console host have no tenant). Each sets only the request-scoped GUCs
 * the relevant 0017 policy branch reads:
 *
 *   createSession          app.operator_id, app.session_host
 *                          -> operator_sessions_self WITH CHECK
 *   resolveSession         app.session_token_hash, app.session_host
 *                          -> operator_sessions_self USING (token-hash branch)
 *   revokeSession          same as resolveSession
 *   revokeAllForOperator   app.operator_id
 *                          -> operator_sessions_self USING (host-blind operator branch)
 *   authenticateOperator   app.login_email, then app.operator_id + app.session_host
 *
 * Token: 256 bits of CSPRNG, base64url, returned once and never recoverable.
 * `operator_sessions.token_hash` and `app.session_token_hash` carry its
 * SHA-256, hex. SHA-256 not scrypt: a uniform 256-bit token has nothing to
 * strengthen, and a KDF would add tens of milliseconds to every request's
 * resolve.
 *
 * Expiry is ABSOLUTE - {@link SESSION_LIFETIME_MS} from creation, no sliding,
 * no refresh. `createSession` on an operator who already has a session mints a
 * new independent row; it never touches or extends the old one. The 0017
 * policy has no expiry clause, so `resolveSession` enforces it against
 * `deps.now()`; expired rows are a scheduled purge's problem, not this
 * module's.
 */
import { createHash, randomBytes } from "node:crypto";
import { sql } from "@osds/db";
import {
  hash as hashPassword,
  verify as verifyPassword,
  needsRehash as passwordNeedsRehash,
} from "../password.js";
import { withAppRole, type Db, type PersistDeps } from "./shared.js";

/** 14 days. Absolute - see the file header. */
const SESSION_LIFETIME_MS = 14 * 24 * 60 * 60 * 1000;

/** 256 bits. */
const TOKEN_BYTES = 32;

/**
 * A valid scrypt hash of a fixed string, at password.ts's CURRENT parameters.
 * {@link authenticateOperator} verifies against it when no operator row matches
 * (#76), so the unknown-email path costs one scrypt like the match path.
 *
 * REGENERATE THIS whenever password.ts CURRENT changes. verifyPassword
 * recomputes at the *stored* parameters, so a dummy left at an older, cheaper
 * cost makes the unknown-email path measurably faster than a real verify -
 * exactly the timing leak #76 exists to close.
 *
 *   node -e "import('@osds/core').then(async m =>
 *     console.log(await m.hashPassword('osds unknown-operator timing equaliser')))"
 */
const DUMMY_HASH =
  "$scrypt$ln=16,r=8,p=1$W0rnHwuxFK0ZwwD4tkv0nQ$CymidrKi/s1PLWG/yx2mTeFKHi19SrAQCIfO+9IAuSE";

export interface Session {
  /** The raw bearer token. Returned once; unrecoverable afterwards. */
  readonly token: string;
  /** Absolute expiry - for the cookie's Max-Age. */
  readonly expiresAt: Date;
}

export interface ResolvedSession {
  readonly operatorId: string;
  readonly expiresAt: Date;
}

/**
 * The SHA-256 (hex) of a raw session token - the value stored in
 * `operator_sessions.token_hash` and carried in the `app.session_token_hash`
 * GUC. SHA-256 not scrypt: a uniform 256-bit token has nothing to strengthen
 * (see the file header). Exported so `@osds/api`'s request-context resolver
 * hashes a presented cookie identically instead of reproducing the algorithm.
 */
export const tokenHashOf = (token: string): string =>
  createHash("sha256").update(token).digest("hex");

function asDate(v: Date | string): Date {
  return v instanceof Date ? v : new Date(v);
}

/**
 * Mint a session for `operatorId`, bound to `host`. Returns the raw token
 * once. Never touches or extends an existing session - absolute expiry means a
 * fresh row every time.
 */
export async function createSession(
  db: Db,
  deps: PersistDeps,
  operatorId: string,
  host: string,
): Promise<Session> {
  const hostLower = host.toLowerCase();
  const token = randomBytes(TOKEN_BYTES).toString("base64url");
  const expiresAt = new Date(deps.now().getTime() + SESSION_LIFETIME_MS);

  await withAppRole(
    db,
    { operatorId, sessionHost: hostLower },
    (trx) =>
      sql`
        insert into operator_sessions
          (id, operator_id, token_hash, issued_for_host, expires_at)
        values (
          ${`ses_${deps.newId()}`}, ${operatorId}, ${tokenHashOf(token)},
          ${hostLower}, ${expiresAt.toISOString()}
        )
      `.execute(trx),
  );

  return { token, expiresAt };
}

/**
 * Resolve a presented token at `host` to its operator, or `null`. Sets
 * `app.session_token_hash` and `app.session_host` before the SELECT (the 0017
 * token-hash branch). `null` covers: no such token, right token at the wrong
 * host, and an expired row (the policy carries no expiry clause, so it is
 * checked here against `deps.now()`).
 */
export async function resolveSession(
  db: Db,
  deps: PersistDeps,
  token: string,
  host: string,
): Promise<ResolvedSession | null> {
  const tokenHash = tokenHashOf(token);

  const row = await withAppRole(
    db,
    { sessionTokenHash: tokenHash, sessionHost: host.toLowerCase() },
    async (trx) => {
      const res = await sql<{ operator_id: string; expires_at: Date | string }>`
        select operator_id, expires_at
        from operator_sessions
        where token_hash = ${tokenHash}
      `.execute(trx);
      return res.rows[0] ?? null;
    },
  );

  if (row === null) return null;
  const expiresAt = asDate(row.expires_at);
  if (expiresAt.getTime() <= deps.now().getTime()) return null;
  return { operatorId: row.operator_id, expiresAt };
}

/** Delete the session a token points at, at `host`. Idempotent. */
export async function revokeSession(
  db: Db,
  token: string,
  host: string,
): Promise<void> {
  const tokenHash = tokenHashOf(token);
  await withAppRole(
    db,
    { sessionTokenHash: tokenHash, sessionHost: host.toLowerCase() },
    (trx) =>
      sql`delete from operator_sessions where token_hash = ${tokenHash}`.execute(
        trx,
      ),
  );
}

/** Delete every session of `operatorId`, on every host ("log out everywhere"). */
export async function revokeAllForOperator(
  db: Db,
  operatorId: string,
): Promise<void> {
  await withAppRole(db, { operatorId }, (trx) =>
    sql`delete from operator_sessions where operator_id = ${operatorId}`.execute(
      trx,
    ),
  );
}

/**
 * Look an operator up by email, verify `password`, opportunistically rehash a
 * stale hash, and mint a session bound to `host`. Returns the session, or
 * `null` on an unknown email or a wrong password - indistinguishably, and in
 * comparable time: an unknown email still runs one scrypt against
 * {@link DUMMY_HASH} (#76).
 *
 * Throws {@link InvalidPasswordHashError} if the stored hash is structurally
 * corrupt - a fault to fix, not a login failure to bounce forever.
 */
export async function authenticateOperator(
  db: Db,
  deps: PersistDeps,
  email: string,
  password: string,
  host: string,
): Promise<Session | null> {
  const emailLower = email.toLowerCase();

  const found = await withAppRole(
    db,
    { loginEmail: emailLower },
    async (trx) => {
      const res = await sql<{ id: string; password_hash: string }>`
        select id, password_hash from operators where email = ${emailLower}
      `.execute(trx);
      return res.rows[0] ?? null;
    },
  );

  // #76: run a hash on the no-match path too, so response time cannot
  // distinguish a registered operator email from an unregistered one.
  const ok = await verifyPassword(password, found?.password_hash ?? DUMMY_HASH);
  if (found === null || !ok) return null;

  if (passwordNeedsRehash(found.password_hash)) {
    try {
      const fresh = await hashPassword(password);
      await withAppRole(db, { operatorId: found.id }, (trx) =>
        sql`update operators set password_hash = ${fresh} where id = ${found.id}`.execute(
          trx,
        ),
      );
    } catch {
      // Opportunistic: a failed rehash must never fail a valid login.
    }
  }

  return createSession(db, deps, found.id, host);
}
