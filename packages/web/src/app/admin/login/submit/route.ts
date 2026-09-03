/**
 * POST /admin/login/submit - the operator login form's action target, for both
 * the tenant admin surface and the console host (decisions.md, "Admin
 * surfaces": two hosts, two independent sessions). The form itself is the
 * server component at GET /admin/login; App Router will not put a page and a
 * route handler on one segment, so the handler sits one level down. Serves both
 * hosts; the only host it refuses is one that resolves to neither a tenant nor
 * the console.
 *
 * `authenticateOperator` verifies the password and mints the session row in one
 * call - there is no separate `createSession` here. Its result is `null` for
 * both an unknown email and a wrong password, in comparable time; a failed
 * login never reveals whether the address has an account.
 *
 * Two callers, two failure shapes, chosen by whether the request `Accept`s
 * text/html:
 *   - A browser form POST is redirected (303) back to GET /admin/login with the
 *     reason in `?error=` (`credentials` or `throttled`), so the page shows it
 *     with no client JS. No interval travels with `throttled`: the window is
 *     ~15 minutes and self-expiring, and a number in the query string would be
 *     attacker-controlled input for the page to parse.
 *   - Anything else keeps the machine contract: a bare `401`, or a `429`
 *     carrying `Retry-After`. Retry-After is the throttle contract for a
 *     non-browser caller; a query string is not.
 * Neither failure differs by which of unknown-email / wrong-password occurred.
 *
 * The `{ throttled: true }` result (#86) leaks nothing about account existence -
 * `authenticateOperator` counts attempts against the *submitted* address, so one
 * that was never an operator trips the limit on the same schedule as a real one.
 *
 * Web `Request` in, Web `Response` out - no `next/*` import. Request primitives
 * come from `lib/request-context` (the one adapter).
 */
import { serializeSessionCookie } from "@osds/api";
import { authenticateOperator, isLoginThrottled } from "@osds/core/persist";
import { getDb } from "../../../../lib/db";
import { persistDeps } from "../../../../lib/persist-deps";
import { getRequestContext } from "../../../../lib/request-context";
import {
  localRedirect,
  sameOriginGuard,
  seeOther,
  text,
} from "../../../../lib/route-helpers";

export async function POST(request: Request): Promise<Response> {
  const blocked = sameOriginGuard(request);
  if (blocked) return blocked;

  // A browser form POST Accepts text/html and wants the page back with any
  // error shown (303 -> GET /admin/login?error=...). A programmatic caller
  // gets the status directly, and Retry-After on a throttle.
  const wantsHtml = (request.headers.get("accept") ?? "").includes("text/html");

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
    return wantsHtml
      ? seeOther("/admin/login?error=missing")
      : text(400, "email and password are required.\n");
  }

  const session = await authenticateOperator(
    getDb(),
    persistDeps,
    email,
    password,
    ctx.host,
  );

  if (isLoginThrottled(session)) {
    if (wantsHtml) return seeOther("/admin/login?error=throttled");
    return new Response("Too many sign-in attempts. Try again later.\n", {
      status: 429,
      headers: {
        "content-type": "text/plain; charset=utf-8",
        "retry-after": String(session.retryAfterSeconds),
      },
    });
  }
  if (session === null) {
    return wantsHtml
      ? seeOther("/admin/login?error=credentials")
      : text(401, "Email or password is incorrect.\n");
  }

  return new Response(null, {
    status: 303,
    headers: {
      Location: localRedirect(form.get("redirect_to")),
      "Set-Cookie": serializeSessionCookie(session, persistDeps),
    },
  });
}
