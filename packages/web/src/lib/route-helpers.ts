/**
 * Small helpers shared by the admin/console route handlers. No `next/*` import -
 * plain Web `Request` / `Response`.
 */
import { normalizeHost, type DispatchOutcome } from "@osds/api";

/** A `text/plain` response with a trailing newline already in `body`. */
export function text(status: number, body: string): Response {
  return new Response(body, {
    status,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function problemJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/problem+json; charset=utf-8" },
  });
}

/**
 * Render a {@link DispatchOutcome} from `@osds/api`'s `dispatchCommand` to an
 * HTTP response. The single place command dispatch meets status codes and
 * `application/problem+json` (spec §7: 202 / 409 / 422, plus 401 / 403 / 500).
 */
export function commandResponse(outcome: DispatchOutcome): Response {
  switch (outcome.kind) {
    case "accepted":
      return json(202, { event_id: outcome.eventId });
    case "duplicate":
      return json(409, { event_id: outcome.eventId });
    case "unauthorized":
      return problemJson(401, {
        type: "https://osds.dev/problems/unauthorized",
        title: "authentication required",
        status: 401,
        code: "unauthorized",
        detail: "sign in at /admin before dispatching a command",
      });
    case "rejected":
    case "unsupported":
    case "forbidden":
    case "error": {
      const status =
        typeof outcome.problem.status === "number"
          ? outcome.problem.status
          : 422;
      return problemJson(status, outcome.problem);
    }
  }
}

/**
 * Reject a state-changing POST unless it is same-origin, to block login-CSRF
 * (an attacker's page submitting a cross-site form that logs the victim into
 * the attacker's account) and cross-site logout.
 *
 * A missing `Origin` on a POST is itself a rejection: every current browser
 * sends `Origin` on form POSTs, so its absence is a non-browser or a stripped
 * request, not something to wave through.
 *
 * The comparison is on the normalized hostname (`@osds/api`'s `normalizeHost`,
 * the same function that shapes the session's `issued_for_host`), not the raw
 * authority: `new URL()` drops a scheme's default port while the `Host` header
 * may keep it, so a verbatim compare would false-reject. Port is not
 * significant here - a same-host different-port peer is already same-site for
 * the `SameSite=Lax` cookie. A reverse proxy must still forward `Host`
 * faithfully.
 *
 * Returns a `403` `Response` to send, or `null` when the request may proceed.
 */
export function sameOriginGuard(request: Request): Response | null {
  const origin = request.headers.get("origin");
  if (origin === null) {
    return text(403, "Missing Origin header on a POST.\n");
  }
  let originHost: string;
  try {
    originHost = new URL(origin).host;
  } catch {
    return text(403, "Malformed Origin header.\n");
  }
  const from = normalizeHost(originHost);
  const host = normalizeHost(request.headers.get("host") ?? "");
  if (from === "" || from !== host) {
    return text(403, "Cross-origin request rejected.\n");
  }
  return null;
}

/**
 * A local redirect target: must be an absolute path on this origin. Anything
 * else - a scheme, a protocol-relative `//host`, a backslash - falls back to
 * `/admin`.
 */
export function localRedirect(raw: FormDataEntryValue | null): string {
  if (typeof raw !== "string") return "/admin";
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) {
    return "/admin";
  }
  return raw;
}
