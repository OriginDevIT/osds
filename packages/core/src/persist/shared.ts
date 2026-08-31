/**
 * Plumbing shared by the `@osds/core/persist` command handlers - the pieces
 * that are identical across `listing.upsert`, `claim.submit` and
 * `claim.approve`: the transaction discipline, the outbox writer, and the
 * idempotency lookup.
 *
 * Nothing here is part of the public `@osds/core/persist` surface.
 */
import { sql } from "@osds/db";
import type { Kysely } from "@osds/db";
import type { OsdsCommand } from "@osds/adapter-kit";

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
 * Insert every event of one command into the outbox, in emission order so
 * per-subject ordering holds (§3.1). `command.idempotency_key` goes on the
 * FIRST row only; the rest get `null`, because the unique index is
 * `(tenant_id, idempotency_key)` and a second non-null copy would collide
 * (see `writeOutbox` in command/handle.ts). Returns the first row's id - the
 * one a replay lookup resolves to.
 *
 * Throws on an empty list: an accepted command that produced no event is a bug,
 * not a silent no-op (cf. the empty-assignment guard in listing-upsert.ts).
 */
export async function writeOutboxEvents(
  trx: Db,
  command: OsdsCommand,
  deps: PersistDeps,
  events: readonly OutboxEvent[],
): Promise<string> {
  if (events.length === 0) {
    throw new Error(
      `${command.command} persistence: an accepted command produced no events`,
    );
  }

  const actor = JSON.stringify({ type: "adapter", id: command.adapter_id });
  const occurredAt = deps.now().toISOString();

  let primaryId = "";
  for (const [i, event] of events.entries()) {
    const id = deps.newId();
    if (i === 0) primaryId = id;

    await sql`
      insert into outbox (
        id, tenant_id, type, version, occurred_at, subject,
        actor, origin, trace_id, data, idempotency_key
      ) values (
        ${id}, ${command.tenant_id}, ${event.type}, 1, ${occurredAt}, ${event.subject},
        ${actor}::jsonb, ${command.adapter_id}, ${command.trace_id},
        ${JSON.stringify(event.data)}::jsonb,
        ${i === 0 ? command.idempotency_key : null}
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
