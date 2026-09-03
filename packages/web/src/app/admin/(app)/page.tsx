/**
 * GET /admin - the authenticated landing for the tenant-admin and console
 * surfaces. Guarded by `(app)/layout.tsx`, so by the time this renders the
 * host has resolved and there is an operator behind the session.
 *
 * Minimal for now: who you are signed in as, and a way out. Real admin
 * navigation and per-surface content land in later PRs.
 *
 * `getRequestContext()` is memoized with React `cache()` (see
 * `lib/request-context.ts`), so calling it again here after the layout already
 * did is one session round trip, not two.
 */
import { getRequestContext } from "../../../lib/request-context";

export const dynamic = "force-dynamic";

export default async function AdminHome() {
  const ctx = await getRequestContext();
  // The layout guaranteed a resolved operator on a real host; this only narrows
  // the union for the type-checker.
  const operator = ctx.kind === "unknown" ? null : ctx.operator;

  return (
    <main>
      <h1>Admin</h1>
      {operator !== null && <p>Signed in as {operator.email}.</p>}
      <form method="post" action="/admin/logout">
        <button type="submit">Sign out</button>
      </form>
    </main>
  );
}
