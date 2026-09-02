/**
 * POST /admin/commands - an operator dispatches a command from `/admin`.
 *
 * Thin: same-origin guard, parse the JSON body, resolve the request context,
 * hand both to `@osds/api`'s `dispatchCommand`, render the {@link
 * DispatchOutcome}. All envelope validation, authorization (surface ->
 * authenticated -> envelope tenant == host tenant -> role rank) and routing
 * live in `dispatchCommand`; the HTTP status mapping lives in `commandResponse`.
 *
 * Web `Request` in, Web `Response` out - no `next/*` import.
 */
import { dispatchCommand } from "@osds/api";
import { getDb } from "../../../lib/db";
import { persistDeps } from "../../../lib/persist-deps";
import { getRequestContext } from "../../../lib/request-context";
import {
  commandResponse,
  sameOriginGuard,
  text,
} from "../../../lib/route-helpers";

export async function POST(request: Request): Promise<Response> {
  const blocked = sameOriginGuard(request);
  if (blocked) return blocked;

  const ctx = await getRequestContext();
  if (ctx.kind === "unknown") {
    return text(404, "Not found.\n");
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return text(400, "Expected a JSON body.\n");
  }

  const outcome = await dispatchCommand(ctx, body, getDb(), persistDeps);
  return commandResponse(outcome);
}
