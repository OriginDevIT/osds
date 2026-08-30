/**
 * 0012_outbox - transactional outbox (spec §11.1).
 *
 * Core writes the event row here in the same transaction as the state change.
 * The worker consumes via LISTEN/NOTIFY on the `osds_outbox` channel, with a
 * polling fallback over the partial `dispatched_at is null` index. `id` is the
 * event ULID and doubles as the idempotency key. `data` is nulled at 90 days
 * by a scheduled job (§11.2); `payload_nulled_at` records when.
 *
 * RLS is uniform with every other table. The outbox consumer is cross-tenant,
 * so it runs with BYPASSRLS or iterates tenants explicitly - see the package
 * README.
 *
 * Rollback:
 *   drop table if exists outbox;
 *   drop function if exists osds_outbox_notify();
 *   Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types.js";
import { enableTenantRls } from "./helpers.js";

export async function up(db: MigrationDb): Promise<void> {
  await sql`
    create table outbox (
      id                text primary key,
      tenant_id         text not null references tenants (id) on delete cascade,
      type              text not null,
      version           integer not null check (version >= 1),
      occurred_at       timestamptz not null,
      subject           text not null,
      actor             jsonb not null,
      origin            text,
      trace_id          text not null,
      data              jsonb not null default '{}',
      created_at        timestamptz not null default now(),
      dispatched_at     timestamptz,
      payload_nulled_at timestamptz
    )
  `.execute(db);

  await sql`
    create index outbox_undispatched on outbox (created_at)
      where dispatched_at is null
  `.execute(db);
  await sql`create index outbox_by_subject on outbox (tenant_id, subject, occurred_at)`.execute(db);
  await sql`create index outbox_by_trace on outbox (trace_id)`.execute(db);

  await sql`
    create function osds_outbox_notify() returns trigger
      language plpgsql
      as $$
      begin
        perform pg_notify('osds_outbox', new.id);
        return null;
      end
      $$
  `.execute(db);

  await sql`
    create trigger outbox_notify after insert on outbox
      for each row execute function osds_outbox_notify()
  `.execute(db);

  await enableTenantRls(db, "outbox");
}
