/**
 * `resolveRequestContext` - the first thing every admin/console request does:
 * turn a Host header and a session cookie into "which surface is this, and who
 * (if anyone) is acting". decisions.md, "Admin surfaces" and "Authentication";
 * migration 0017.
 *
 * A library, not a server (decisions.md, "`packages/api` is a library"). It
 * imports no `next/*` module and never touches a `Request`: the caller - a thin
 * `packages/web` adapter - reads the Host header, the `__Host-` cookie value and
 * the configured console hostname, and passes them as strings. Same injection
 * rule as the clock and the id factory in `@osds/core`.
 *
 * Three outcomes (discriminated on `kind`):
 *
 *   tenant   - the Host matched a `tenants.domain`. `/admin` lives here. Carries
 *              `tenantId` and, when a session resolved, the operator plus their
 *              ACTIVE membership role for this tenant (`role: null` if they hold
 *              none - e.g. a pending invite, which confers nothing per §4.4).
 *   console  - the Host is the configured console host. Resolves to no tenant
 *              (decisions.md), so the operator carries no role. Every operator
 *              reaches the console - accepting a pending invite happens here.
 *   unknown  - matched no tenant domain and is not the console host. The caller
 *              renders 404. No session lookup is attempted: a session is
 *              host-bound, so there is nothing to resolve, and a bogus Host
 *              header cannot probe for session existence.
 *
 * GUC order inside the one transaction (migration 0017, "Request order"):
 * `app.session_token_hash` (+ `app.session_host`, the only lowercased GUC) ->
 * SELECT the session -> `app.operator_id` from that row -> `app.tenant_id`
 * (tenant surface only, for the membership read). Set progressively, so a
 * later GUC is never in scope before the SELECT that justifies it.
 *
 * The `operator_sessions_self` policy carries no expiry clause, so the session
 * SELECT enforces it with `expires_at > now()` (transaction time).
 *
 * Read-only: opens a transaction only because `SET LOCAL` needs one.
 */
import { sql, type Kysely } from "@osds/db";
import { tokenHashOf } from "@osds/core/persist";
import type { StaffRole } from "@osds/core";

/** An operator whose host-bound session resolved. */
export interface ResolvedOperator {
  /** `op_` ULID. */
  readonly operatorId: string;
  /** Lowercased in storage. */
  readonly email: string;
  /** Installation scope (spec §4.4). A separate axis from `role`. */
  readonly isSuperadmin: boolean;
}

/** On a tenant surface: the operator, plus their active membership role here. */
export interface TenantOperator extends ResolvedOperator {
  /**
   * The operator's ACTIVE `staff_memberships.role` for this tenant, or `null`
   * when they hold no active membership on it (a `pending` invite included -
   * §4.4, "confers nothing until accepted").
   */
  readonly role: StaffRole | null;
}

export interface TenantContext {
  readonly kind: "tenant";
  /** Lowercased, port stripped. */
  readonly host: string;
  /** `tnt_` ULID. Resolved from the Host header, before the session check. */
  readonly tenantId: string;
  /** `null` when the request carried no session, or it did not resolve. */
  readonly operator: TenantOperator | null;
}

export interface ConsoleContext {
  readonly kind: "console";
  readonly host: string;
  /** `null` when the request carried no session, or it did not resolve. */
  readonly operator: ResolvedOperator | null;
}

export interface UnknownHostContext {
  readonly kind: "unknown";
  readonly host: string;
}

export type RequestContext = TenantContext | ConsoleContext | UnknownHostContext;

export interface RequestInput {
  /** Raw Host header value. Compared lowercased; a `:port` and IPv6 brackets are tolerated. */
  readonly host: string;
  /** Raw `__Host-` session cookie value, or `null` when the request carried none. */
  readonly sessionToken: string | null;
  /** The deployment's configured console hostname. Compared lowercased. */
  readonly consoleHost: string;
}

/**
 * Host header value -> bare hostname: trimmed, lowercased, port stripped, IPv6
 * brackets kept. The single host normalizer - `packages/web` uses this exact
 * function for the `host` it hands `authenticateOperator` / `createSession` /
 * `revokeSession`, so a session's `issued_for_host` is stored under the same
 * form this resolver later matches on.
 */
export function normalizeHost(raw: string): string {
  const value = raw.trim().toLowerCase();
  if (value === "") return "";
  if (value.startsWith("[")) {
    const close = value.indexOf("]");
    return close === -1 ? value : value.slice(0, close + 1);
  }
  const colon = value.indexOf(":");
  return colon === -1 ? value : value.slice(0, colon);
}

/**
 * `tenants` has no `tenant_id` and no RLS (migration 0002), so this runs with
 * no role set and no `app.tenant_id` scope - exactly as the public site
 * resolves its tenant.
 */
async function tenantIdForDomain(
  db: Kysely<unknown>,
  host: string,
): Promise<string | null> {
  const res = await sql<{ id: string }>`
    select id from tenants where domain = ${host} limit 1
  `.execute(db);
  return res.rows[0]?.id ?? null;
}

interface SessionOperator {
  readonly base: ResolvedOperator;
  /** The active role for `tenantId`, or `null`; always `null` when `tenantId` was `null`. */
  readonly role: StaffRole | null;
}

/**
 * Resolve a presented cookie to its operator (and, for a tenant surface, that
 * operator's active role), or `null`. One transaction as `osds_app`, GUCs set
 * in the 0017 "Request order".
 */
async function resolveSessionOperator(
  db: Kysely<unknown>,
  sessionToken: string | null,
  host: string,
  tenantId: string | null,
): Promise<SessionOperator | null> {
  if (sessionToken === null || sessionToken === "") return null;
  const tokenHash = tokenHashOf(sessionToken);

  return db.transaction().execute(async (trx) => {
    await sql`set local role osds_app`.execute(trx);

    // (1) token hash + host, BEFORE the session SELECT: operator_sessions_self's
    //     token-hash branch reads both. app.session_host is the only lowercased
    //     GUC (osds_current_session_host() lowercases too); `host` is already
    //     normalized, so the explicit WHERE below matches issued_for_host.
    await sql`select set_config('app.session_token_hash', ${tokenHash}, true)`.execute(
      trx,
    );
    await sql`select set_config('app.session_host', ${host}, true)`.execute(trx);

    const session = await sql<{ operator_id: string }>`
      select operator_id
      from operator_sessions
      where token_hash = ${tokenHash}
        and issued_for_host = ${host}
        and expires_at > now()
      limit 1
    `.execute(trx);
    const operatorId = session.rows[0]?.operator_id ?? null;
    if (operatorId === null) return null;

    // (2) the resolved operator id - operators_read and the staff_memberships
    //     self branch key on it.
    await sql`select set_config('app.operator_id', ${operatorId}, true)`.execute(
      trx,
    );

    const operator = await sql<{ email: string; is_superadmin: boolean }>`
      select email, is_superadmin
      from operators
      where id = ${operatorId}
      limit 1
    `.execute(trx);
    const row = operator.rows[0];
    // A session row whose operator is gone (mid-request cascade delete): treat
    // as unauthenticated rather than throw.
    if (row === undefined) return null;
    const base: ResolvedOperator = {
      operatorId,
      email: row.email,
      isSuperadmin: row.is_superadmin,
    };

    if (tenantId === null) return { base, role: null };

    // (3) app.tenant_id LAST - tenant-scoped work only (0017, "Request order").
    //     Scopes the active-membership read below.
    await sql`select set_config('app.tenant_id', ${tenantId}, true)`.execute(trx);

    const membership = await sql<{ role: StaffRole }>`
      select role
      from staff_memberships
      where operator_id = ${operatorId}
        and tenant_id = ${tenantId}
        and status = 'active'
      limit 1
    `.execute(trx);
    return { base, role: membership.rows[0]?.role ?? null };
  });
}

/**
 * Resolve a request's host and session cookie to a {@link RequestContext}. See
 * the file header for the three outcomes and the transaction's GUC order.
 */
export async function resolveRequestContext(
  input: RequestInput,
  db: Kysely<unknown>,
): Promise<RequestContext> {
  const host = normalizeHost(input.host);
  const consoleHost = normalizeHost(input.consoleHost);

  // Console host first: it is a reserved name, so a `tenants.domain` that
  // happens to equal it must not shadow the console login (decisions.md).
  if (host !== "" && host === consoleHost) {
    const resolved = await resolveSessionOperator(
      db,
      input.sessionToken,
      host,
      null,
    );
    return { kind: "console", host, operator: resolved?.base ?? null };
  }

  const tenantId = host === "" ? null : await tenantIdForDomain(db, host);
  if (tenantId === null) {
    return { kind: "unknown", host };
  }

  const resolved = await resolveSessionOperator(
    db,
    input.sessionToken,
    host,
    tenantId,
  );
  const operator: TenantOperator | null =
    resolved === null ? null : { ...resolved.base, role: resolved.role };
  return { kind: "tenant", host, tenantId, operator };
}
