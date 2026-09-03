import { cache } from "react";
import { cookies, headers } from "next/headers";
import {
  resolveRequestContext,
  SESSION_COOKIE_NAME,
  type RequestContext,
} from "@osds/api";
import { CONSOLE_HOST } from "./config";
import { getDb } from "./db";

/**
 * The single `next/*` request-primitive coupling point for admin/console auth.
 * `next/headers` is confined here; route handlers take a Web `Request` and
 * return a Web `Response`.
 *
 * `resolveRequestContext` owns host normalization and the session lookup; this
 * only reads the `Host` header and the `__Host-` cookie and forwards them as
 * strings, with the configured console host.
 *
 * Wrapped in React `cache()`: the `admin/(app)` layout runs the session check
 * and the page it wraps re-reads the same context, so without memoization every
 * guarded `/admin` request would do two session round trips. `cache` dedupes
 * them within one render pass; route handlers, which render outside React, are
 * unaffected and still resolve once each.
 */
export const getRequestContext = cache(
  async (): Promise<RequestContext> => {
    const [headerList, cookieStore] = await Promise.all([headers(), cookies()]);
    return resolveRequestContext(
      {
        host: headerList.get("host") ?? "",
        sessionToken: cookieStore.get(SESSION_COOKIE_NAME)?.value ?? null,
        consoleHost: CONSOLE_HOST,
      },
      getDb(),
    );
  },
);

/** The raw session cookie value for this request, or `null`. Logout needs it. */
export async function getSessionToken(): Promise<string | null> {
  const cookieStore = await cookies();
  return cookieStore.get(SESSION_COOKIE_NAME)?.value ?? null;
}
