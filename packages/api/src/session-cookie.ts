/**
 * Operator session cookie serialization - decisions.md, "Authentication"
 * (Transport row) and "Admin surfaces".
 *
 * Two pure string builders. No `next/*` import, no `Request`/`Response`, no
 * framework cookie jar: `packages/web` writes the returned string to a
 * `Set-Cookie` header, and reads the value back for {@link
 * resolveRequestContext} using {@link SESSION_COOKIE_NAME}.
 *
 * Transport decision: HttpOnly, Secure, SameSite=Lax, `__Host-` prefix. The
 * browser enforces `__Host-`'s three conditions, which are exactly the shape we
 * want:
 *   - `Secure` set,
 *   - `Path=/` exactly,
 *   - NO `Domain` attribute.
 * Host-only binding is the mechanism the "two hosts, two independent sessions"
 * decision relies on - a cookie set on the console host is never sent to a
 * tenant host, and vice versa.
 *
 * No dev fallback that drops `Secure` or the prefix. Chrome and Firefox treat
 * `http://localhost` as a secure context, so `Secure` / `__Host-` cookies are
 * set and returned over plain-HTTP localhost unchanged. A `NODE_ENV`-gated
 * branch would be dead weight that ships to production if the check is ever
 * wrong, and would make dev and prod exercise different cookie paths.
 *
 * `Max-Age` is derived from `expiresAt` against an injected clock - never a
 * hardcoded lifetime. The clock is injected, not imported, so the pure layer
 * cannot quietly acquire `Date.now()` (same rule as `@osds/core`'s
 * `PersistDeps`).
 */

/**
 * The one session cookie name. `as const` so it types as the literal
 * `"__Host-osds_session"`, not `string`: a consumer that declares a parameter
 * as `typeof SESSION_COOKIE_NAME` rejects any other hardcoded string at
 * typecheck.
 */
export const SESSION_COOKIE_NAME = "__Host-osds_session" as const;

/**
 * The minimum a session needs to become a cookie. Width-compatible with
 * `@osds/core/persist`'s `Session` on purpose - the web adapter passes a
 * `createSession` / `authenticateOperator` result straight through. A test
 * asserts that assignability at typecheck.
 */
export interface SessionCookieInput {
  /** The raw bearer token. Written verbatim - see {@link assertCookieSafe}. */
  readonly token: string;
  /** Absolute session expiry. `Max-Age` is computed from this and the clock. */
  readonly expiresAt: Date;
}

/** Injected wall clock. Never imported here. */
export interface CookieClock {
  readonly now: () => Date;
}

/** Attributes shared by the live and the cleared cookie, so the two cannot drift. */
const FIXED_ATTRS = "Path=/; HttpOnly; Secure; SameSite=Lax";

/**
 * RFC 6265 cookie-octet:
 *   %x21 / %x23-2B / %x2D-3A / %x3C-5B / %x5D-7E
 * i.e. printable ASCII minus SP, DQUOTE, comma, semicolon, and backslash.
 * Core mints the token as base64url (`[A-Za-z0-9_-]`), which is well inside
 * this set; the guard defends against a future token format, not today's.
 */
const COOKIE_OCTET = /^[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]*$/;

function assertCookieSafe(token: string): void {
  if (!COOKIE_OCTET.test(token)) {
    throw new Error(
      "serializeSessionCookie: token contains a character not allowed in a cookie value",
    );
  }
}

/** Whole seconds until `expiresAt`, floored, never negative (a past expiry -> 0 -> delete). */
function maxAgeSeconds(expiresAt: Date, now: Date): number {
  return Math.max(
    0,
    Math.floor((expiresAt.getTime() - now.getTime()) / 1000),
  );
}

/**
 * `Set-Cookie` value for a freshly minted session:
 *
 *   __Host-osds_session=<token>; Max-Age=<n>; Path=/; HttpOnly; Secure; SameSite=Lax
 *
 * `<n>` is `expiresAt - deps.now()` in whole seconds. The token is written
 * unescaped (base64url is cookie-safe); a token outside the cookie-octet set
 * throws.
 */
export function serializeSessionCookie(
  session: SessionCookieInput,
  deps: CookieClock,
): string {
  assertCookieSafe(session.token);
  const maxAge = maxAgeSeconds(session.expiresAt, deps.now());
  return `${SESSION_COOKIE_NAME}=${session.token}; Max-Age=${maxAge}; ${FIXED_ATTRS}`;
}

/**
 * `Set-Cookie` value that deletes the session cookie:
 *
 *   __Host-osds_session=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax
 *
 * Constant, no clock. `Secure` and `Path=/` are still required: a browser
 * rejects a `__Host-` `Set-Cookie` without them, and a rejected deletion leaves
 * the live cookie in place.
 */
export function serializeClearedSessionCookie(): string {
  return `${SESSION_COOKIE_NAME}=; Max-Age=0; ${FIXED_ATTRS}`;
}
