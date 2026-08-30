/**
 * 0014_outbox_idempotency_key - command idempotency (spec §7).
 *
 * §7 requires a replayed command (same `idempotency_key`) to collapse to one
 * effect and return the original event id. That key is adapter-supplied,
 * derived from external identifiers, and distinct from the event `id`. Record
 * it on the outbox row the command produced. The partial unique index makes the
 * replay a cheap lookup and blocks a concurrent double-apply - the losing
 * INSERT gets 23505 and the caller re-reads the original id.
 *
 * Core-originated events carry no key, hence nullable + partial index.
 *
 * Rollback:
 *   drop index if exists outbox_idempotency;
 *   alter table outbox drop column if exists idempotency_key;
 *   Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types.js";

export async function up(db: MigrationDb): Promise<void> {
  await sql`alter table outbox add column idempotency_key text`.execute(db);
  await sql`
    create unique index outbox_idempotency
      on outbox (tenant_id, idempotency_key)
      where idempotency_key is not null
  `.execute(db);
}
