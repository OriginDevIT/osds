/**
 * POST /admin/logout - revoke the current session and clear the cookie.
 * Idempotent: always `303` + a cleared `__Host-` cookie when the host is a real
 * surface, whether or not a valid session existed. `revokeSession` deletes 0 or
 * 1 rows.
 *
 * POST, not GET, so a prefetch or a cross-site link cannot log a user out;
 * `sameOriginGuard` covers cross-site form POSTs.
 *
 * Web `Request` in, Web `Response` out - no `next/*` import.
 */
import { serializeClearedSessionCookie } from "@osds/api";
import { revokeSession } from "@osds/core/persist";
import { getDb } from "../../../lib/db";
import { getRequestContext, getSessionToken } from "../../../lib/request-context";
import { sameOriginGuard, text } from "../../../lib/route-helpers";

export async function POST(request: Request): Promise<Response> {
  const blocked = sameOriginGuard(request);
  if (blocked) return blocked;

  const ctx = await getRequestContext();
  if (ctx.kind === "unknown") {
    return text(404, "Not found.\n");
  }

  const token = await getSessionToken();
  if (token !== null) {
    await revokeSession(getDb(), token, ctx.host);
  }

  return new Response(null, {
    status: 303,
    headers: {
      Location: "/admin/login",
      "Set-Cookie": serializeClearedSessionCookie(),
    },
  });
}
