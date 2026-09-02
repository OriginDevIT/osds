/**
 * `@osds/api` - the request-handling library. Next route handlers and server
 * components in `packages/web` call into it; there is no second HTTP process
 * (decisions.md, "`packages/api` is a library, not a server"). It imports no
 * `next/*` module - request primitives are read by a thin `packages/web`
 * adapter and passed in as strings.
 *
 * Today: request-context resolution (host -> tenant / console / unknown, plus
 * the operator and their active role when a session resolves). Command dispatch
 * into `@osds/core/persist` and RFC 7807 responses land here next.
 */
export {
  resolveRequestContext,
  type RequestContext,
  type RequestInput,
  type TenantContext,
  type ConsoleContext,
  type UnknownHostContext,
  type ResolvedOperator,
  type TenantOperator,
} from "./request-context.js";

export {
  SESSION_COOKIE_NAME,
  serializeSessionCookie,
  serializeClearedSessionCookie,
  type SessionCookieInput,
  type CookieClock,
} from "./session-cookie.js";

// Spec §4.4 role rules, re-exported for request-context consumers. They
// originate in `@osds/core` - a spec rule with no driver - not here.
export { ROLE_RANK, type StaffRole } from "@osds/core";
