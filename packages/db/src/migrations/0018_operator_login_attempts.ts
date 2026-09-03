/**
 * 0018_operator_login_attempts - a per-email login-attempt limit for
 * POST /admin/login (issue #86).
 *
 * One narrow counter table. `authenticateOperator` (@osds/core/persist) runs a
 * single upsert per login - `insert ... on conflict do update set failures =
 * failures + 1 returning failures` - and refuses the login (HTTP 429 at the
 * route) once the returned count exceeds 5 within the current window, BEFORE it
 * looks the operator up and before any scrypt. A successful login deletes the
 * rows for that email.
 *
 * A single statement, not a read then a separate increment: two concurrent
 * requests that both read a count under the limit would both proceed and both
 * run scrypt - the CPU cost the limit exists to cap. The upsert makes the
 * count-and-test atomic.
 *
 * Fixed 15-minute windows. `window_start` is the epoch floored to a 900s
 * boundary, computed by the caller from its injected clock, so the bucket is
 * wall-clock aligned and a refused attempt cannot push the boundary forward.
 * `failures` counts every attempt in the bucket, not only failed ones - the
 * name is the issue's wording; a success removes the row before the count
 * matters, and counting attempts is what keeps the boundary fixed.
 *
 * No `tenant_id`. Like `operators` and `operator_sessions` this is a
 * principal-scoped table, grouped with `tenants` by the invariant-3
 * clarification in CLAUDE.md - NOT the §11.2 `command_log` exception, which is
 * for rows whose tenant cannot be resolved. Scoping an attempt counter to a
 * tenant would let one host's failures be replayed against another tenant's
 * host under the same address.
 *
 * Keyed on the SHA-256 of the lowercased *submitted* address (hex in the GUC,
 * bytea in the column), never a resolved operator id: an address that was never
 * an operator must throttle on the same schedule as a real one, so the 429
 * cannot be turned into an existence oracle.
 *
 * No pepper on the digest. A pepper defends a hash against offline guessing,
 * but `operators.email` is already stored in plaintext in this same database -
 * the only addresses this hash conceals are the ones that appear here and
 * nowhere else (typos, credential-stuffing scans against addresses that are not
 * operators). Against anyone who can already read `operators`, a pepper on this
 * column buys nothing. The digest still earns its place: without it the table
 * is a second, wider plaintext register of every address ever tried, with its
 * own retention duty and readable from any backup.
 *
 * --- RLS ---------------------------------------------------------------------
 *
 * Forced. There is no session when this check runs (it precedes the operator
 * lookup) and no tenant (the table is principal-scoped), so the policy is not
 * authorization - there is no principal to authorize. It is containment: a bug
 * or an injected query on the login path must not be able to read the whole
 * register or clear another account's lockout. The only value the caller holds
 * pre-auth is the address being attempted, so the policy is self-scoping on its
 * digest, carried in `app.login_attempt_hash` and resolved by
 * `osds_current_login_attempt_hash()` (unset -> NULL -> `email_hash = NULL` ->
 * no row -> default deny, the 0001 / 0017 idiom; `decode` is strict, so a NULL
 * setting yields a NULL bytea and matches nothing).
 *
 * One `for all` policy, `operator_login_attempts_self`, predicate
 * `email_hash = decode(osds_current_login_attempt_hash(), 'hex')` as both USING
 * and WITH CHECK. An unexplained `for all` is a bug this repo has already
 * shipped, so each of the four commands it covers is spelled out, and why the
 * one predicate is right for it:
 *
 *   SELECT  The upsert's RETURNING reads `failures` back, and RETURNING is held
 *           to the SELECT policy. The returned row is the one whose `email_hash`
 *           equals the presented digest, so USING passes it. `osds_app` issues
 *           no other SELECT against this table - the counter is never listed or
 *           scanned.
 *   INSERT  WITH CHECK: the new row's `email_hash` must equal the presented
 *           digest. A caller cannot seed a counter under a different address.
 *   UPDATE  Reached only through ON CONFLICT DO UPDATE. USING locates the
 *           conflicting row by the same predicate; WITH CHECK holds the updated
 *           row to it. `email_hash` is never in the SET list, so a row cannot be
 *           moved onto another digest.
 *   DELETE  The success path clears every window for the address. USING confines
 *           it to rows under the presented digest.
 *
 * The single predicate fits all four because the table has exactly one access
 * pattern: act on the row(s) for the address this request is attempting,
 * identified by a digest the caller must already hold.
 *
 * Rollback:
 *   drop policy if exists operator_login_attempts_self on operator_login_attempts;
 *   revoke all on operator_login_attempts from osds_app;
 *   drop index if exists operator_login_attempts_by_window;
 *   drop table if exists operator_login_attempts;
 *   drop function if exists osds_current_login_attempt_hash();
 *   Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types.js";

export async function up(db: MigrationDb): Promise<void> {
  // Request-scoped GUC resolver, cf. osds_current_tenant_id (0001) and the 0017
  // resolvers: unset / '' -> NULL, so every policy branch reading it default-denies.
  await sql`
    create function osds_current_login_attempt_hash() returns text
      language sql
      stable
      as $$ select nullif(current_setting('app.login_attempt_hash', true), '') $$
  `.execute(db);

  await sql`
    create table operator_login_attempts (
      email_hash    bytea       not null,
      window_start  timestamptz not null,
      failures      integer     not null default 0 check (failures >= 0),
      primary key (email_hash, window_start)
    )
  `.execute(db);

  // Housekeeping only - lets a scheduled purge drop windows older than one
  // bucket without a sequential scan. The hot path hits the primary key.
  await sql`
    create index operator_login_attempts_by_window
      on operator_login_attempts (window_start)
  `.execute(db);

  await sql`
    grant select, insert, update, delete on operator_login_attempts to osds_app
  `.execute(db);

  await sql`alter table operator_login_attempts enable row level security`.execute(
    db,
  );
  await sql`alter table operator_login_attempts force row level security`.execute(
    db,
  );

  await sql`
    create policy operator_login_attempts_self on operator_login_attempts
      for all
        using (email_hash = decode(osds_current_login_attempt_hash(), 'hex'))
        with check (email_hash = decode(osds_current_login_attempt_hash(), 'hex'))
  `.execute(db);
}
