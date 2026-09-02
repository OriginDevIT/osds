/**
 * Plumbing shared by the `@osds/core/persist` command handlers - the pieces
 * that are identical across `listing.upsert`, `claim.submit` and
 * `claim.approve`: the transaction discipline, the outbox writer, the
 * idempotency lookup, and the §11.2 command log.
 *
 * Nothing here is part of the public `@osds/core/persist` surface.
 */
import { sql } from "@osds/db";
import type { Kysely } from "@osds/db";
import type {
  ActorType,
  OsdsCommand,
  ProblemDocument,
} from "@osds/adapter-kit";
import { validationProblem } from "../command/problem.js";
import { ROLE_RANK, type StaffRole } from "../roles.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- generated schema types are not wired up yet
export type Db = Kysely<any>;

/** Injected effects. Never imported into a persist module - the resolvers stay pure. */
export interface PersistDeps {
  /** Wall clock, for each event's `occurred_at`. */
  readonly now: () => Date;
  /** ULID factory. Entity prefixes (`listing_`, `claim_`, `usr_`) are added by the caller. */
  readonly newId: () => string;
}

/**
 * Run `fn` in one transaction as `osds_app` (so RLS is enforced - the role is
 * NOBYPASSRLS and not the table owner) with `app.tenant_id` set for its
 * duration. Both settings are transaction-local and reset on commit/rollback.
 */
export function withTenant<T>(
  db: Db,
  tenantId: string,
  fn: (trx: Db) => Promise<T>,
): Promise<T> {
  return db.transaction().execute(async (trx) => {
    await sql`set local role osds_app`.execute(trx);
    await sql`select set_config('app.tenant_id', ${tenantId}, true)`.execute(
      trx,
    );
    return fn(trx);
  });
}

/**
 * Request-scoped GUCs a NON-tenant persist transaction may set (operator auth,
 * sessions - the console host has no tenant at all). An omitted key is left
 * unset: its 0017 resolver returns NULL, and every policy branch reading it
 * default-denies, so a missing GUC fails closed rather than widening.
 */
export interface AppGucs {
  readonly operatorId?: string;
  readonly loginEmail?: string;
  readonly sessionTokenHash?: string;
  readonly sessionHost?: string;
}

const APP_GUC_NAME: Readonly<Record<keyof AppGucs, string>> = {
  operatorId: "app.operator_id",
  loginEmail: "app.login_email",
  sessionTokenHash: "app.session_token_hash",
  sessionHost: "app.session_host",
};

/**
 * {@link withTenant} without the tenant: one transaction as `osds_app` with the
 * given request-scoped GUCs set transaction-local. Four lines overlap with
 * `withTenant` on purpose - the command path is not worth touching to share
 * them. GUC *names* are fixed here; only the values come from the caller, so
 * `set_config` (which takes the name as a value, not an identifier) cannot be
 * steered.
 */
export function withAppRole<T>(
  db: Db,
  gucs: AppGucs,
  fn: (trx: Db) => Promise<T>,
): Promise<T> {
  return db.transaction().execute(async (trx) => {
    await sql`set local role osds_app`.execute(trx);
    for (const key of Object.keys(APP_GUC_NAME) as (keyof AppGucs)[]) {
      const value = gucs[key];
      if (value !== undefined) {
        await sql`select set_config(${APP_GUC_NAME[key]}, ${value}, true)`.execute(
          trx,
        );
      }
    }
    return fn(trx);
  });
}

/** The first outbox row id for `idempotency_key` in this tenant, or `null`. */
export async function findEventId(
  trx: Db,
  tenantId: string,
  idempotencyKey: string,
): Promise<string | null> {
  const res = await sql<{ id: string }>`
    select id from outbox
    where tenant_id = ${tenantId} and idempotency_key = ${idempotencyKey}
    limit 1
  `.execute(trx);
  return res.rows[0]?.id ?? null;
}

export interface OutboxEvent {
  readonly type: string;
  readonly subject: string;
  readonly data: unknown;
}

/**
 * Who a command attributes its events to. The caller supplies one at every
 * {@link writeOutboxEvents} call - there is no default. A silent default is how
 * an `adapter` label ended up permanently stamped on staff actions (#95).
 *
 *   adapter  - an adapter's command. `actor.type` is `"adapter"`, and `origin`
 *              is the adapter id (the §2.1 loop guard).
 *   operator - a signed-in operator dispatching from `/admin`. `actor.type` is
 *              `"admin"` when the membership role is `admin` (spec §4.4,
 *              "Reading actor.type"), `"staff"` otherwise. Not adapter-caused,
 *              so `origin` is `null`.
 *   admin    - an admin id carried in a command payload rather than a session
 *              (`entitlement.grant.admin_id`). That command has its own writer
 *              in `command/handle.ts`; this arm is here so the eventual fold has
 *              a name. `origin` is `null`.
 *
 * No `system` arm yet - a scheduled job that needs one adds it with a decision.
 */
export type CommandActor =
  | { readonly kind: "adapter"; readonly adapterId: string }
  | {
      readonly kind: "operator";
      readonly operatorId: string;
      readonly role: StaffRole;
    }
  | { readonly kind: "admin"; readonly adminId: string };

/**
 * A {@link CommandActor} resolved to what the outbox row stores: the `actor`
 * object (§2 envelope) and the §2.1 `origin` loop guard. `origin` is the adapter
 * id only for an adapter command; anything not adapter-caused gets `null`.
 */
function resolveActor(actor: CommandActor): {
  readonly actor: { readonly type: ActorType; readonly id: string };
  readonly origin: string | null;
} {
  switch (actor.kind) {
    case "adapter":
      return {
        actor: { type: "adapter", id: actor.adapterId },
        origin: actor.adapterId,
      };
    case "operator":
      return {
        actor: {
          type: ROLE_RANK[actor.role] >= ROLE_RANK.admin ? "admin" : "staff",
          id: actor.operatorId,
        },
        origin: null,
      };
    case "admin":
      return {
        actor: { type: "admin", id: actor.adminId },
        origin: null,
      };
  }
}

/**
 * Insert every event of one command into the outbox, in emission order so
 * per-subject ordering holds (§3.1). `command.idempotency_key` goes on exactly
 * one row - the event at `keyIndex` (default 0, the first) - and the rest get
 * `null`, because the unique index is `(tenant_id, idempotency_key)` and a
 * second non-null copy would collide (see `writeOutbox` in command/handle.ts).
 * Returns that row's id - the one a replay lookup resolves to.
 *
 * `keyIndex` is non-zero only when an earlier event is emitted ahead of the
 * command's headline fact: `claim.submit` emits `user.created` before
 * `claim.submitted` (§4.3), but the idempotency key and the returned id stay on
 * `claim.submitted`.
 *
 * `actor` ({@link CommandActor}) is supplied by the caller - there is no
 * default. It sets each row's `actor` and, together, its `origin` (#95).
 *
 * Throws on an empty list: an accepted command that produced no event is a bug,
 * not a silent no-op (cf. the empty-assignment guard in listing-upsert.ts).
 */
export async function writeOutboxEvents(
  trx: Db,
  command: OsdsCommand,
  deps: PersistDeps,
  actor: CommandActor,
  events: readonly OutboxEvent[],
  keyIndex = 0,
): Promise<string> {
  if (events.length === 0) {
    throw new Error(
      `${command.command} persistence: an accepted command produced no events`,
    );
  }
  if (keyIndex < 0 || keyIndex >= events.length) {
    throw new Error(
      `${command.command} persistence: keyIndex ${keyIndex} out of range for ${events.length} events`,
    );
  }

  const resolved = resolveActor(actor);
  const actorJson = JSON.stringify(resolved.actor);
  const occurredAt = deps.now().toISOString();

  let primaryId = "";
  for (const [i, event] of events.entries()) {
    const id = deps.newId();
    if (i === keyIndex) primaryId = id;

    await sql`
      insert into outbox (
        id, tenant_id, type, version, occurred_at, subject,
        actor, origin, trace_id, data, idempotency_key
      ) values (
        ${id}, ${command.tenant_id}, ${event.type}, 1, ${occurredAt}, ${event.subject},
        ${actorJson}::jsonb, ${resolved.origin}, ${command.trace_id},
        ${JSON.stringify(event.data)}::jsonb,
        ${i === keyIndex ? command.idempotency_key : null}
      )
    `.execute(trx);
  }

  return primaryId;
}

export function isUniqueViolation(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { code?: string }).code === "23505"
  );
}

// --- command log (§11.2) --------------------------------------------

/** A logged, un-concluded command-log row awaiting {@link concludeCommandLog}. */
export interface CommandLogHandle {
  readonly id: string;
  /** Always a resolved tenant - the null-tenant path never yields a handle. */
  readonly tenant_id: string;
}

export type CommandLogBegin =
  | { readonly kind: "open"; readonly handle: CommandLogHandle }
  | { readonly kind: "closed"; readonly problem: ProblemDocument };

/** How a command settled - one persist result, widened for the log. */
export interface CommandLogConclusion {
  readonly status: string;
  readonly event_id?: string;
  readonly problem?: ProblemDocument;
}

/**
 * Record a command attempt in its own transaction, committed BEFORE the command
 * transaction opens, so a rollback or a crash mid-apply still leaves a durable
 * row (§11.2 - "a rejected command otherwise leaves no trace"). Runs as
 * `osds_app` with the tenant GUC set, exactly like {@link withTenant}.
 *
 *   - Tenant resolves against `tenants` -> insert an un-concluded row and
 *     return `{ kind: "open" }` with a handle for {@link concludeCommandLog}.
 *   - Tenant does not resolve (missing, malformed, or unknown) -> there is no
 *     command to run, so insert a single already-concluded `rejected` row
 *     (`tenant_id` null) and return `{ kind: "closed" }` with the problem. The
 *     caller returns that rejection without opening a command transaction, and
 *     never has to update or read a null-tenant row.
 */
export async function beginCommandLog(
  db: Db,
  command: OsdsCommand,
  deps: PersistDeps,
): Promise<CommandLogBegin> {
  const id = `cmd_${deps.newId()}`;
  const rawTenant =
    typeof command.tenant_id === "string" && command.tenant_id
      ? command.tenant_id
      : null;
  const payload =
    command.payload === undefined || command.payload === null
      ? null
      : JSON.stringify(command.payload);
  const nowIso = deps.now().toISOString();

  const tenantId =
    rawTenant !== null && (await tenantExists(db, rawTenant))
      ? rawTenant
      : null;

  if (tenantId === null) {
    const problem = validationProblem(
      `command names an unresolvable tenant "${rawTenant ?? ""}"`,
    );
    // No GUC: `command_log_insert` allows a null-tenant row unconditionally.
    await db.transaction().execute(async (trx) => {
      await sql`set local role osds_app`.execute(trx);
      await sql`
        insert into command_log (
          id, tenant_id, command, adapter_id, idempotency_key, trace_id,
          payload, outcome, problem, received_at, concluded_at
        ) values (
          ${id}, null, ${asText(command.command)},
          ${asTextOrNull(command.adapter_id)}, ${asTextOrNull(command.idempotency_key)},
          ${asTextOrNull(command.trace_id)}, ${payload}::jsonb,
          'rejected', ${JSON.stringify(problem)}::jsonb, ${nowIso}, ${nowIso}
        )
      `.execute(trx);
    });
    return { kind: "closed", problem };
  }

  await withTenant(db, tenantId, async (trx) => {
    await sql`
      insert into command_log (
        id, tenant_id, command, adapter_id, idempotency_key, trace_id,
        payload, received_at
      ) values (
        ${id}, ${tenantId}, ${asText(command.command)},
        ${asTextOrNull(command.adapter_id)}, ${asTextOrNull(command.idempotency_key)},
        ${asTextOrNull(command.trace_id)}, ${payload}::jsonb, ${nowIso}
      )
    `.execute(trx);
  });
  return { kind: "open", handle: { id, tenant_id: tenantId } };
}

/**
 * Conclude the row once the command transaction has settled: its `outcome`,
 * `concluded_at`, and per outcome the `problem` document or the first emitted
 * `event_id`. Runs as `osds_app` with the GUC set; `command_log_update` only
 * touches an un-concluded row of that tenant, so a command that threw (never
 * reaching here) keeps its null outcome and a concluded row stays frozen.
 */
export async function concludeCommandLog(
  db: Db,
  handle: CommandLogHandle,
  deps: PersistDeps,
  result: CommandLogConclusion,
): Promise<void> {
  await withTenant(db, handle.tenant_id, async (trx) => {
    await sql`
      update command_log set
        outcome = ${result.status},
        event_id = ${result.event_id ?? null},
        problem = ${result.problem ? JSON.stringify(result.problem) : null}::jsonb,
        concluded_at = ${deps.now().toISOString()}
      where id = ${handle.id}
    `.execute(trx);
  });
}

async function tenantExists(db: Db, id: string): Promise<boolean> {
  const res = await sql<{ one: number }>`
    select 1 as one from tenants where id = ${id} limit 1
  `.execute(db);
  return res.rows.length > 0;
}

function asText(v: unknown): string {
  return typeof v === "string" ? v : String(v ?? "");
}

function asTextOrNull(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}
