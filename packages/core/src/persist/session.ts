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
 *   authenticateOperator   app.login_attempt_hash (the attempt counter, 0018 /
 *                          #86), then app.login_email, then - via createSession -
 *                          app.operator_id + app.session_host
 *
 * authenticateOperator also enforces a per-email login-attempt limit (#86).
 * Every call counts one attempt in a fixed 15-minute window with a single
 * upsert on `operator_login_attempts` and, once the running count exceeds
 * {@link LOGIN_ATTEMPT_LIMIT}, returns {@link LoginThrottled} carrying the
 * seconds left in the window - BEFORE the operator lookup and before any
 * scrypt, so a flood cannot spend the box's CPU. One statement, not a read then
 * an increment: two concurrent requests both under the limit would otherwise
 * both proceed to scrypt. The counter is keyed on the SHA-256 of the submitted
 * address, never a resolved operator id, so an address that was never an
 * operator throttles identically and the 429 is not an existence oracle. A
 * successful login deletes the rows.
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

/** The fixed login-attempt bucket (#86): 15 minutes, in ms. */
const LOGIN_ATTEMPT_WINDOW_MS = 15 * 60 * 1000;

/**
 * Attempts per {@link LOGIN_ATTEMPT_WINDOW_MS} window before a login is
 * throttled. The attempt that makes the running count exceed this is the first
 * one refused, so this many attempts per window reach password verification.
 */
const LOGIN_ATTEMPT_LIMIT = 5;

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

/**
 * {@link authenticateOperator}'s third outcome (#86): the login was refused
 * before credentials were checked because too many attempts have been made for
 * this email in the current fixed window. `null` still means an unknown email
 * or a wrong password.
 *
 * `throttled: true` is a literal discriminant, matching `@osds/api`'s
 * `DispatchOutcome` - see {@link isLoginThrottled}.
 */
export interface LoginThrottled {
  readonly throttled: true;
  /** Whole seconds until the current window ends. In `(0, 900]`. */
  readonly retryAfterSeconds: number;
}

/** Narrows {@link authenticateOperator}'s result to {@link LoginThrottled}. */
export const isLoginThrottled = (
  result: Session | LoginThrottled | null,
): result is LoginThrottled => result !== null && "throttled" in result;

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
 * Before any of that (#86): count this attempt in the current fixed 15-minute
 * window with one upsert, and if the running count exceeds
 * {@link LOGIN_ATTEMPT_LIMIT} return {@link LoginThrottled} - no operator
 * lookup, no scrypt. The count is per submitted address (hashed), so an address
 * that is not an operator throttles on the same schedule. A successful login
 * clears every window for the address.
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
): Promise<Session | LoginThrottled | null> {
  const emailLower = email.toLowerCase();
  const emailHashHex = createHash("sha256").update(emailLower).digest("hex");

  const now = deps.now();
  const windowStart = new Date(
    Math.floor(now.getTime() / LOGIN_ATTEMPT_WINDOW_MS) *
      LOGIN_ATTEMPT_WINDOW_MS,
  );

  // One statement: count this attempt and read the running total back. A read
  // then a separate increment races - two requests both under the limit would
  // both pass and both run scrypt, the CPU cost the limit exists to cap (#86).
  // Counting every attempt (not just failures) is safe: the window is a fixed
  // wall-clock bucket, so a refused attempt cannot push the boundary out.
  const attempts = await withAppRole(
    db,
    { loginAttemptHash: emailHashHex },
    async (trx) => {
      const res = await sql<{ failures: number }>`
        insert into operator_login_attempts (email_hash, window_start, failures)
        values (decode(${emailHashHex}, 'hex'), ${windowStart.toISOString()}, 1)
        on conflict (email_hash, window_start)
          do update set failures = operator_login_attempts.failures + 1
        returning failures
      `.execute(trx);
      return res.rows[0]!.failures;
    },
  );

  if (attempts > LOGIN_ATTEMPT_LIMIT) {
    const retryAfterSeconds = Math.ceil(
      (windowStart.getTime() + LOGIN_ATTEMPT_WINDOW_MS - now.getTime()) / 1000,
    );
    return { throttled: true, retryAfterSeconds };
  }

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

  // Successful login: clear every window for this email, so a stale earlier
  // bucket cannot throttle the next sign-in.
  await withAppRole(db, { loginAttemptHash: emailHashHex }, (trx) =>
    sql`delete from operator_login_attempts where email_hash = decode(${emailHashHex}, 'hex')`.execute(
      trx,
    ),
  );

  return createSession(db, deps, found.id, host);
}
