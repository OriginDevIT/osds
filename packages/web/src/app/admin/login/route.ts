/**
 * POST /admin/login - operator login for both the tenant admin surface and the
 * console host (decisions.md, "Admin surfaces": two hosts, two independent
 * sessions). Serves both; the only host it refuses is one that resolves to
 * neither a tenant nor the console.
 *
 * `authenticateOperator` verifies the password and mints the session row in one
 * call - there is no separate `createSession` here. Its result is `null` for
 * both an unknown email and a wrong password, in comparable time; this handler
 * maps `null` to one fixed `401`, so a failed login never reveals whether the
 * address has an account.
 *
 * The `{ throttled: true }` result (too many recent attempts for this email,
 * #86) is the one deliberate exception to that identical-failed-login rule: it
 * returns `429` with `Retry-After`. It still leaks nothing about account
 * existence - `authenticateOperator` counts attempts against the *submitted*
 * address, so one that was never an operator trips the limit on the same
 * schedule as a real one.
 *
 * Web `Request` in, Web `Response` out - no `next/*` import. Request primitives
 * come from `lib/request-context` (the one adapter).
 */
import { serializeSessionCookie } from "@osds/api";
import { authenticateOperator, isLoginThrottled } from "@osds/core/persist";
import { getDb } from "../../../lib/db";
import { persistDeps } from "../../../lib/persist-deps";
import { getRequestContext } from "../../../lib/request-context";
import { localRedirect, sameOriginGuard, text } from "../../../lib/route-helpers";

export async function POST(request: Request): Promise<Response> {
  const blocked = sameOriginGuard(request);
  if (blocked) return blocked;

  const ctx = await getRequestContext();
  if (ctx.kind === "unknown") {
    // Not a surface a session can exist on. Refuse before any credential work:
    // no DB round trip, no scrypt, no timing signal about which hosts are real.
    return text(404, "Not found.\n");
  }

  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    return text(400, "Expected a form submission.\n");
  }
  const email = String(form.get("email") ?? "").trim();
  const password = String(form.get("password") ?? "");
  if (email === "" || password === "") {
    return text(400, "email and password are required.\n");
  }

  const session = await authenticateOperator(
    getDb(),
    persistDeps,
    email,
    password,
    ctx.host,
  );
  if (isLoginThrottled(session)) {
    return new Response("Too many sign-in attempts. Try again later.\n", {
      status: 429,
      headers: {
        "content-type": "text/plain; charset=utf-8",
        "retry-after": String(session.retryAfterSeconds),
      },
    });
  }
  if (session === null) {
    return text(401, "Email or password is incorrect.\n");
  }

  return new Response(null, {
    status: 303,
    headers: {
      Location: localRedirect(form.get("redirect_to")),
      "Set-Cookie": serializeSessionCookie(session, persistDeps),
    },
  });
}
