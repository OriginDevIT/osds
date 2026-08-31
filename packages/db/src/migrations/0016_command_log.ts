/**
 * 0016_command_log - the second of the three §11.2 logs (issue #41).
 *
 * Every command attempt is recorded here, including rejected and blocked ones:
 * "the agent attempted X and was blocked" is precisely the record worth
 * having, and a rejected command otherwise leaves no trace. The row is written
 * on its own connection and committed BEFORE the command transaction opens, so
 * a rollback or a crash mid-apply still leaves a durable trace. `osds_app`
 * writes it - no owner connection - so RLS has to permit that (below).
 *
 * `tenant_id` is nullable - the deliberate exception to invariant 3. A
 * malformed command whose tenant does not resolve still has to leave a trace;
 * such a row carries `tenant_id = null` and is written already concluded (there
 * is no command to run). The FK is single-column and, per the SQL standard's
 * MATCH SIMPLE, is not enforced while that column is null.
 *
 * `outcome` null means the attempt was recorded but never concluded (crash
 * mid-apply); it is set when the command settles, along with `concluded_at`
 * and either `problem` (a rejection) or `event_id` (the first emitted event).
 * `payload` is nulled at 90 days by a scheduled job (§11.2) - that job is not
 * part of this migration.
 *
 * RLS: forced.
 *   - `command_log_select` scopes reads to `app.tenant_id`. A null `tenant_id`
 *     matches no GUC, so a malformed command's payload is visible only to a
 *     role that bypasses RLS (the owner) - `osds_app` never reads another
 *     tenant's failed command.
 *   - `command_log_insert` lets `osds_app` write a row for its own tenant or
 *     for a null tenant (the invariant-3 exception).
 *   - `command_log_update` lets `osds_app` conclude one un-concluded row of its
 *     own tenant and nothing else: once `concluded_at` is set the row is frozen.
 *     An audit trail the app can rewrite is not an audit trail. The layer never
 *     updates a null-tenant row, so it never needs to read one back.
 *
 * Rollback:
 *   drop index if exists command_log_by_received;
 *   drop policy if exists command_log_update on command_log;
 *   drop policy if exists command_log_insert on command_log;
 *   drop policy if exists command_log_select on command_log;
 *   revoke all on command_log from osds_app;
 *   drop table if exists command_log;
 *   Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types.js";

export async function up(db: MigrationDb): Promise<void> {
  await sql`
    create table command_log (
      id              text primary key check (starts_with(id, 'cmd_')),
      tenant_id       text,
      command         text not null,
      adapter_id      text,
      idempotency_key text,
      trace_id        text,
      payload         jsonb,
      outcome         text,
      problem         jsonb,
      event_id        text,
      received_at     timestamptz not null,
      concluded_at    timestamptz,
      foreign key (tenant_id) references tenants (id) on delete cascade
    )
  `.execute(db);

  await sql`
    create index command_log_by_received
      on command_log (tenant_id, received_at desc)
  `.execute(db);

  await sql`
    grant select, insert, update, delete on command_log to osds_app
  `.execute(db);

  await sql`alter table command_log enable row level security`.execute(db);
  await sql`alter table command_log force row level security`.execute(db);

  await sql`
    create policy command_log_select on command_log
      for select using (tenant_id = osds_current_tenant_id())
  `.execute(db);
  await sql`
    create policy command_log_insert on command_log
      for insert with check (
        tenant_id = osds_current_tenant_id() or tenant_id is null
      )
  `.execute(db);
  await sql`
    create policy command_log_update on command_log
      for update
        using (
          tenant_id = osds_current_tenant_id() and concluded_at is null
        )
        with check (tenant_id = osds_current_tenant_id())
  `.execute(db);
}
