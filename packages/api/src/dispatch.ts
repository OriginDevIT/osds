/**
 * `dispatchCommand` - the operator/admin command path. A signed-in operator
 * submits a command through `/admin`; `resolveRequestContext` has already
 * produced the tenant, the operator and their role. This validates the
 * envelope, authorizes the operator, routes to `@osds/core/persist`, and
 * returns a {@link DispatchOutcome} the HTTP layer renders (spec §7: 202 with
 * the event id, 409 on an idempotency replay carrying the original event id,
 * 422 with an RFC 7807 problem document; plus 401/403/500 the HTTP surface
 * adds - decisions.md, `scopeProblem` is "advisory ... 403 for a scope
 * denial").
 *
 * A discriminated union, not a `Response` - the caller in `packages/web`
 * renders it with `commandResponse`. In-process callers (a future adapter
 * runtime, tests) consume the union directly, the way the spec's
 * `CommandClient.send` returns `CommandResult`.
 *
 * The §11.2 command log is written by each `@osds/core/persist` entrypoint, not
 * here - re-logging would double-log, and `beginCommandLog`/`concludeCommandLog`
 * are not exported.
 *
 * Dispatchable today: `listing.upsert` and `claim.approve`. `claim.submit`
 * needs a claim-config schema for `enabledMethods` (#63); both entitlement
 * commands need a self-logging core wrapper (#96); `entitlement.reportPayment`
 * is refused on this path regardless - adapters own money (§6, invariant 2).
 * Everything else is `unsupported`.
 */
import type {
  CommandName,
  OsdsCommand,
  ProblemDocument,
} from "@osds/adapter-kit";
import { ROLE_RANK } from "@osds/core";
import type { Kysely } from "@osds/db";
import {
  persistClaimApprove,
  persistListingUpsert,
  type CommandActor,
  type PersistClaimApproveResult,
  type PersistDeps,
  type PersistListingUpsertResult,
} from "@osds/core/persist";
import type { RequestContext } from "./request-context.js";

/**
 * Minimum role rank for each command dispatchable through `/admin`, from spec
 * §4.4: `editor` (rank 2) may "edit listing content" and "approve claims".
 * Keyed by exactly the dispatchable set - a command absent here is `unsupported`
 * and never reaches the rank gate.
 */
export const COMMAND_MIN_RANK = {
  "listing.upsert": ROLE_RANK.editor,
  "claim.approve": ROLE_RANK.editor,
} as const satisfies Partial<Record<CommandName, number>>;

type DispatchableCommand = keyof typeof COMMAND_MIN_RANK;

/** Commands that never reach this path, at any role. */
const REFUSED_COMMANDS: ReadonlySet<CommandName> = new Set([
  "entitlement.reportPayment",
]);

function isDispatchable(command: string): command is DispatchableCommand {
  return command in COMMAND_MIN_RANK;
}

export type DispatchOutcome =
  /** 202. `eventId` is `null` for an `unchanged` result - still accepted. */
  | { readonly kind: "accepted"; readonly eventId: string | null }
  /** 409. The original event id; the caller treats it as success (§7). */
  | { readonly kind: "duplicate"; readonly eventId: string }
  /** 422 (or `problem.status`). Malformed envelope, or a business-rule rejection. */
  | { readonly kind: "rejected"; readonly problem: ProblemDocument }
  /** 422. A real command with no dispatch path here yet. */
  | { readonly kind: "unsupported"; readonly problem: ProblemDocument }
  /** 401. No operator resolved. */
  | { readonly kind: "unauthorized" }
  /** 403. Wrong surface, wrong tenant, insufficient role, or a refused command. */
  | { readonly kind: "forbidden"; readonly problem: ProblemDocument }
  /** 500. An `@osds/core/persist` call threw (e.g. a command-log write failed). */
  | { readonly kind: "error"; readonly problem: ProblemDocument };

function problem(
  status: number,
  code: string,
  detail: string,
  errors?: readonly string[],
): ProblemDocument {
  return {
    type: `https://osds.dev/problems/${code.replace(/_/g, "-")}`,
    title: code.replace(/_/g, " "),
    status,
    code,
    detail,
    ...(errors && errors.length > 0 ? { errors: [...errors] } : {}),
  };
}

/** A trimmed non-empty string, or `null`. */
function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() !== "" ? v : null;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

type ParsedEnvelope =
  | { readonly ok: true; readonly command: OsdsCommand }
  | { readonly ok: false; readonly problem: ProblemDocument };

/**
 * Shape-validate the request body and build the {@link OsdsCommand}. The
 * per-command payload is validated later by the core resolver, not here.
 * `idempotency_key` must come from the client (one per submission, so a
 * double-submit collapses); `trace_id` is minted when absent; `adapter_id` is
 * `null` - an operator is not an adapter (spec §7.1), so `command_log.adapter_id`
 * is null and the events carry `origin: null` / `actor: staff|admin` (#95).
 */
function parseEnvelope(body: unknown, deps: PersistDeps): ParsedEnvelope {
  if (!isPlainObject(body)) {
    return {
      ok: false,
      problem: problem(
        422,
        "malformed_envelope",
        "request body must be a JSON object",
      ),
    };
  }

  const errors: string[] = [];
  const command = str(body["command"]);
  if (command === null) errors.push("command is required");
  const idempotencyKey = str(body["idempotency_key"]);
  if (idempotencyKey === null) {
    errors.push("idempotency_key is required - mint one per submission");
  }
  const tenantId = str(body["tenant_id"]);
  if (tenantId === null) errors.push("tenant_id is required");
  if (!isPlainObject(body["payload"])) errors.push("payload must be an object");

  if (
    command === null ||
    idempotencyKey === null ||
    tenantId === null ||
    errors.length > 0
  ) {
    return {
      ok: false,
      problem: problem(
        422,
        "malformed_envelope",
        "the command envelope is malformed",
        errors,
      ),
    };
  }

  return {
    ok: true,
    command: {
      command: command as CommandName,
      idempotency_key: idempotencyKey,
      tenant_id: tenantId,
      adapter_id: null,
      trace_id: str(body["trace_id"]) ?? deps.newId(),
      payload: body["payload"] as Record<string, unknown>,
    },
  };
}

async function runDispatchable(
  command: DispatchableCommand,
  db: Kysely<unknown>,
  envelope: OsdsCommand,
  deps: PersistDeps,
  actor: CommandActor,
): Promise<PersistListingUpsertResult | PersistClaimApproveResult> {
  switch (command) {
    case "listing.upsert":
      return persistListingUpsert(db, envelope, deps, actor);
    case "claim.approve":
      return persistClaimApprove(db, envelope, deps, actor);
  }
}

function mapResult(
  result: PersistListingUpsertResult | PersistClaimApproveResult,
): DispatchOutcome {
  switch (result.status) {
    case "created":
    case "updated":
    case "approved":
      return { kind: "accepted", eventId: result.event_id };
    case "unchanged":
      return { kind: "accepted", eventId: null };
    case "duplicate":
      return { kind: "duplicate", eventId: result.event_id };
    case "rejected":
      return { kind: "rejected", problem: result.problem };
  }
}

/**
 * @param context  from `resolveRequestContext`
 * @param body     the parsed JSON request body
 * @param db       the `osds_app` pool (`@osds/core/persist` opens its own transactions)
 * @param deps     clock + id factory (`ulidFactory`)
 */
export async function dispatchCommand(
  context: RequestContext,
  body: unknown,
  db: Kysely<unknown>,
  deps: PersistDeps,
): Promise<DispatchOutcome> {
  // Gate 1 - surface. Commands are tenant-scoped; the console resolves to no
  // tenant, and an unknown host is not a surface at all.
  if (context.kind !== "tenant") {
    return {
      kind: "forbidden",
      problem: problem(
        403,
        "not_a_tenant_surface",
        context.kind === "console"
          ? "commands are dispatched at a tenant's own /admin; the console host has no tenant"
          : "this host resolves to no tenant",
      ),
    };
  }

  // Gate 2 - authenticated.
  if (context.operator === null) {
    return { kind: "unauthorized" };
  }
  const operator = context.operator;

  const parsed = parseEnvelope(body, deps);
  if (!parsed.ok) return { kind: "rejected", problem: parsed.problem };
  const envelope = parsed.command;

  // Routing: dispatchable, explicitly refused, or not-yet-supported.
  if (!isDispatchable(envelope.command)) {
    if (REFUSED_COMMANDS.has(envelope.command)) {
      return {
        kind: "forbidden",
        problem: problem(
          403,
          "command_refused",
          `"${envelope.command}" is never dispatched by an operator - adapters own money (§6)`,
        ),
      };
    }
    return {
      kind: "unsupported",
      problem: problem(
        422,
        "command_not_supported",
        `"${envelope.command}" is not dispatchable through /admin yet`,
      ),
    };
  }

  // Gate 3 - the envelope's tenant must be the host's tenant.
  //
  // This is the ONLY cross-tenant barrier on the operator path, not
  // defense-in-depth: `@osds/core/persist` opens its transaction with
  // `withTenant(db, command.tenant_id, ...)`, which sets `app.tenant_id` from
  // the envelope. RLS then scopes every write to whatever that GUC says, so it
  // trusts the caller - remove this check and an operator could name any tenant
  // and the write would land there. `dispatch.test.ts` proves it with a
  // direct-persist call ("RLS does not stop this - the gate does").
  if (envelope.tenant_id !== context.tenantId) {
    return {
      kind: "forbidden",
      problem: problem(
        403,
        "tenant_mismatch",
        `command names tenant "${envelope.tenant_id}" but this host is tenant "${context.tenantId}"`,
      ),
    };
  }

  // Gate 4 - role rank. A null role (no active membership) never passes, and a
  // superadmin does not bypass it (decisions.md).
  const minRank = COMMAND_MIN_RANK[envelope.command];
  if (operator.role === null || ROLE_RANK[operator.role] < minRank) {
    return {
      kind: "forbidden",
      problem: problem(
        403,
        "role_insufficient",
        `"${envelope.command}" requires role rank ${minRank}; ` +
          `you hold ${operator.role ?? "no active membership on this tenant"}`,
      ),
    };
  }

  const actor: CommandActor = {
    kind: "operator",
    operatorId: operator.operatorId,
    role: operator.role,
  };

  try {
    return mapResult(
      await runDispatchable(envelope.command, db, envelope, deps, actor),
    );
  } catch {
    // Includes a §11.2 command-log write failure inside `@osds/core/persist`.
    // The effect may or may not have committed; a retry with the same
    // idempotency_key resolves to a 409 if it did.
    return {
      kind: "error",
      problem: problem(
        500,
        "dispatch_failed",
        "the command could not be completed",
      ),
    };
  }
}
