/**
 * Session guard for the tenant-admin and console surfaces (#34; decisions.md
 * "Admin surfaces"). Every page under `admin/(app)/` renders only for a request
 * whose host resolves and whose `__Host-` session resolves to an operator.
 *
 * `/admin/login` sits OUTSIDE this route group (it is `app/admin/login/`, a
 * sibling), so the redirect below never loops back through the guard. Route
 * handlers - `/admin/login/submit`, `/admin/logout`, `/admin/commands` - are
 * `route.ts` files, which layouts do not wrap, so they keep their own checks.
 *
 * Authentication only. On a tenant host `ctx.operator` may carry `role: null` -
 * a valid operator with no active membership here (a pending invite, or a
 * superadmin who has not self-granted; spec §4.4). That operator passes this
 * guard. Per-tenant authorization - the `role === null` experience and rank
 * checks for individual pages - is a later layer; command writes are already
 * rank-gated by `@osds/api`'s `dispatchCommand`.
 *
 * `redirect_to` (returning the operator to where they were headed after login)
 * is deliberately not threaded here - the guard sends everyone to a bare
 * `/admin/login`. Tracked in #107.
 */
import type { ReactNode } from "react";
import { notFound, redirect } from "next/navigation";
import { getRequestContext } from "../../../lib/request-context";

// Reads the Host header and the session cookie - never prerendered.
export const dynamic = "force-dynamic";

export default async function AdminLayout({
  children,
}: {
  children: ReactNode;
}) {
  const ctx = await getRequestContext();
  // Same 404 surface as the login page and the route handlers.
  if (ctx.kind === "unknown") notFound();
  // No session, or it did not resolve: send them to sign in.
  if (ctx.operator === null) redirect("/admin/login");
  return <>{children}</>;
}
