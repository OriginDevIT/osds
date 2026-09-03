/**
 * GET /admin/login - the operator sign-in form, for both the tenant admin
 * surface and the console host (decisions.md, "Admin surfaces"). A server
 * component with no client JS: a plain HTML form that POSTs to
 * /admin/login/submit, which is the handler (App Router will not put a page and
 * a route on one segment).
 *
 * On a failed submission the handler redirects back here with `?error=`; this
 * page renders one fixed string per code. `credentials` is deliberately the
 * same message whether the email was unknown or the password wrong - the
 * handler cannot tell them apart and must not. `throttled` carries no interval:
 * the window is ~15 minutes and self-expiring.
 */
import { notFound, redirect } from "next/navigation";
import { getRequestContext } from "../../../lib/request-context";

// Reads the Host header and the session cookie (via getRequestContext) and the
// query string - never prerendered.
export const dynamic = "force-dynamic";

const ERROR_MESSAGE: Record<string, string> = {
  credentials: "Email or password is incorrect.",
  throttled: "Too many sign-in attempts. Please wait a few minutes.",
  missing: "Enter an email and password.",
};

export default async function AdminLoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const ctx = await getRequestContext();
  // Same 404 surface as the handler: a host that is neither a tenant nor the
  // console has no login page.
  if (ctx.kind === "unknown") notFound();
  // Already signed in on this host - nothing to do here.
  if (ctx.operator !== null) redirect("/admin");

  const rawError = (await searchParams).error;
  const errorKey = typeof rawError === "string" ? rawError : undefined;
  const message =
    errorKey !== undefined ? (ERROR_MESSAGE[errorKey] ?? null) : null;

  return (
    <main>
      <h1>Sign in</h1>
      {message !== null && <p role="alert">{message}</p>}
      <form method="post" action="/admin/login/submit">
        <label>
          Email
          <input type="email" name="email" required autoComplete="username" />
        </label>
        <label>
          Password
          <input
            type="password"
            name="password"
            required
            autoComplete="current-password"
          />
        </label>
        <button type="submit">Sign in</button>
      </form>
    </main>
  );
}
