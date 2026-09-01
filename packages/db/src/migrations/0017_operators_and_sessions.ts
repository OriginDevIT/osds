/**
 * 0017_operators_and_sessions - deployment operators, tenant staff, and
 * operator login sessions (issue #69). Migration and generated types only: no
 * persist functions, no route handlers, no scrypt code.
 *
 * Principals (decisions.md, "Authentication"): `operators` holds the deployment
 * operator and every tenant's staff; `staff_memberships (operator_id,
 * tenant_id, role)` is the tenant relationship. `users` (0005) is a different
 * principal - listing owners and claimants - and is untouched. One operator may
 * administer many tenants, so `operators` and `operator_sessions` carry no
 * `tenant_id`; the invariant-3 clarification in CLAUDE.md groups them with
 * `tenants`. `staff_memberships` carries `tenant_id` because it *is* that
 * relationship.
 *
 * Three request-scoped GUCs, each read by a helper that mirrors
 * `osds_current_tenant_id()` (0001) - `nullif(current_setting(x, true), '')`,
 * so an unset GUC is NULL and `col = NULL` makes every policy default-deny.
 * `osds_operator_admins_current_tenant()` folds two of them into the
 * write-authorization predicate.
 *
 *   app.login_email          set before the login SELECT - the caller names the
 *                            principal it is authenticating. Not an
 *                            authorization gate (see below).
 *   app.operator_id          the authenticated operator, set from the resolved
 *                            session row and used for every later read/write.
 *   app.session_token_hash   the hash of the bearer cookie the request carried.
 *                            The cookie is >= 256 bits of CSPRNG and the app
 *                            hashes it before it reaches Postgres; the raw token
 *                            never enters a GUC or a column. `operator_sessions.
 *                            id` is a `ses_` ULID handle, not the secret.
 *
 * Request order: no GUC -> set app.session_token_hash from the cookie hash ->
 * SELECT the session -> set app.operator_id from the row -> (only for
 * tenant-scoped work, after the staff_memberships authorization check) set
 * app.tenant_id. `packages/web` renders public pages as `osds_app` with only
 * app.tenant_id set and no operator, so every policy branch that keys on a
 * tenant is additionally gated on `osds_current_operator_id() is not null`: a
 * public render can neither read a membership nor pass a session delete.
 *
 * Staff provisioning is a POST-WIZARD operation. The first-run wizard mints
 * exactly one operator - the deployment operator - and its route 404s forever
 * once `operators` is non-empty (decisions.md). Every later operator is tenant
 * staff, created by an already-authenticated operator through the admin
 * surface. So `app.login_email` is not an authorization gate - whatever can set
 * it could mint any operator - and the write policies key on the acting
 * operator holding an `admin` membership for `app.tenant_id`, via
 * `osds_operator_admins_current_tenant()`. Fixing only the `operators` INSERT
 * would be cosmetic: the grant of access is the `staff_memberships` row, so its
 * WITH CHECK carries the same predicate.
 *
 * Two rows therefore cannot be written by `osds_app` and are provisioned by the
 * owner (`DATABASE_URL_ADMIN`), which the wizard and tenant-creation flow
 * already need for pre-auth writes to `tenants` / config / secrets:
 *   - the first operator (wizard), and
 *   - the first `admin` membership of a newly created tenant.
 * The API layer (#4/#5) may instead open a self-closing `staff_memberships`
 * INSERT branch - a self `admin` membership into a tenant that has zero
 * memberships - but that is not this migration.
 *
 * operators - RLS forced.
 *   operators_read       SELECT your own row, or the row whose email is
 *                        app.login_email (the login lookup, before any operator
 *                        is known).
 *   operators_provision  INSERT only when the acting operator is an admin of
 *                        app.tenant_id (staff invite). The wizard's first
 *                        operator is owner-provisioned.
 *   operators_self_write UPDATE your own row (rehash-on-login raises the scrypt
 *                        parameters embedded in `password_hash`).
 *   No DELETE policy: operator removal is not a decided flow, so `osds_app`
 *   cannot delete an operator row yet.
 *
 * staff_memberships - RLS forced, and NOT via enableTenantRls: that shared
 *   `tenant_isolation` policy is granted to PUBLIC and would let a public page
 *   render read every tenant's staff list.
 *   staff_memberships_visible  USING: your own memberships (operator_id branch),
 *     or - only while acting as an operator - the memberships of the tenant in
 *     app.tenant_id. WITH CHECK: the acting operator is an admin of
 *     app.tenant_id, so only a tenant admin adds staff or changes a role. The
 *     first admin of a tenant is owner-provisioned (above).
 *
 * operator_sessions - RLS forced. Rows are immutable: absolute `expires_at`, no
 *   sliding refresh, no UPDATE grant, no UPDATE policy.
 *   operator_sessions_self           the session whose token_hash the request
 *     presented (resolution, logout), or every session of the authenticated
 *     operator (session list, log-out-everywhere).
 *   operator_sessions_tenant_revoke  DELETE only: `tenant.suspended` deletes
 *     every session of every operator with a membership in app.tenant_id in one
 *     statement, as `osds_app`, gated on there being an operator so a public
 *     render cannot trigger it. The subquery reads `staff_memberships` under its
 *     own RLS, so it needs app.tenant_id set to the suspended tenant and an
 *     operator context.
 *
 * Rollback:
 *   drop policy if exists operator_sessions_tenant_revoke on operator_sessions;
 *   drop policy if exists operator_sessions_self on operator_sessions;
 *   drop policy if exists staff_memberships_visible on staff_memberships;
 *   drop policy if exists operators_self_write on operators;
 *   drop policy if exists operators_provision on operators;
 *   drop policy if exists operators_read on operators;
 *   drop function if exists osds_operator_admins_current_tenant();
 *   revoke all on operator_sessions from osds_app;
 *   revoke all on staff_memberships from osds_app;
 *   revoke all on operators from osds_app;
 *   drop table if exists operator_sessions;
 *   drop table if exists staff_memberships;
 *   drop table if exists operators;
 *   drop function if exists osds_current_session_token_hash();
 *   drop function if exists osds_current_operator_id();
 *   drop function if exists osds_current_login_email();
 *   Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types.js";
import { touchUpdatedAt } from "./helpers.js";

export async function up(db: MigrationDb): Promise<void> {
  // --- request-scoped GUC resolvers (cf. osds_current_tenant_id, 0001) ------
  await sql`
    create function osds_current_operator_id() returns text
      language sql
      stable
      as $$ select nullif(current_setting('app.operator_id', true), '') $$
  `.execute(db);
  await sql`
    create function osds_current_login_email() returns text
      language sql
      stable
      as $$ select nullif(current_setting('app.login_email', true), '') $$
  `.execute(db);
  await sql`
    create function osds_current_session_token_hash() returns text
      language sql
      stable
      as $$ select nullif(current_setting('app.session_token_hash', true), '') $$
  `.execute(db);

  // --- operators ----------------------------------------------------------
  await sql`
    create table operators (
      id             text primary key check (starts_with(id, 'op_')),
      email          text not null unique check (email = lower(email)),
      password_hash  text not null,
      created_at     timestamptz not null default now(),
      updated_at     timestamptz not null default now()
    )
  `.execute(db);

  await sql`grant select, insert, update, delete on operators to osds_app`.execute(
    db,
  );
  await sql`alter table operators enable row level security`.execute(db);
  await sql`alter table operators force row level security`.execute(db);

  await sql`
    create policy operators_read on operators
      for select using (
        id = osds_current_operator_id()
        or email = osds_current_login_email()
      )
  `.execute(db);
  await sql`
    create policy operators_self_write on operators
      for update
        using (id = osds_current_operator_id())
        with check (id = osds_current_operator_id())
  `.execute(db);
  // operators_provision is created below, once staff_memberships exists - it
  // keys on an admin membership, not on app.login_email.

  await touchUpdatedAt(db, "operators");

  // --- staff_memberships ------------------------------------------------
  await sql`
    create table staff_memberships (
      operator_id  text not null references operators (id) on delete cascade,
      tenant_id    text not null references tenants (id) on delete cascade,
      role         text not null check (role in ('admin', 'staff')),
      created_at   timestamptz not null default now(),
      updated_at   timestamptz not null default now(),
      primary key (operator_id, tenant_id)
    )
  `.execute(db);

  await sql`
    create index staff_memberships_by_tenant on staff_memberships (tenant_id)
  `.execute(db);

  await sql`grant select, insert, update, delete on staff_memberships to osds_app`.execute(
    db,
  );
  await sql`alter table staff_memberships enable row level security`.execute(db);
  await sql`alter table staff_memberships force row level security`.execute(db);

  // True when the acting operator holds an `admin` membership for the tenant in
  // app.tenant_id. Gates who may provision an operator or a membership. NULL
  // GUCs -> no match -> false, so a public render (app.tenant_id only, no
  // operator) never passes.
  await sql`
    create function osds_operator_admins_current_tenant() returns boolean
      language sql
      stable
      as $$
        select exists (
          select 1 from staff_memberships
          where operator_id = osds_current_operator_id()
            and tenant_id   = osds_current_tenant_id()
            and role = 'admin'
        )
      $$
  `.execute(db);

  await sql`
    create policy operators_provision on operators
      for insert with check (osds_operator_admins_current_tenant())
  `.execute(db);

  await sql`
    create policy staff_memberships_visible on staff_memberships
      for all
        using (
          operator_id = osds_current_operator_id()
          or (
            osds_current_operator_id() is not null
            and tenant_id = osds_current_tenant_id()
          )
        )
        with check (osds_operator_admins_current_tenant())
  `.execute(db);

  await touchUpdatedAt(db, "staff_memberships");

  // --- operator_sessions ----------------------------------------------
  await sql`
    create table operator_sessions (
      id           text primary key check (starts_with(id, 'ses_')),
      operator_id  text not null references operators (id) on delete cascade,
      token_hash   text not null unique,
      expires_at   timestamptz not null,
      created_at   timestamptz not null default now()
    )
  `.execute(db);

  await sql`
    create index operator_sessions_by_operator
      on operator_sessions (operator_id)
  `.execute(db);

  // Immutable rows. 0013's blanket ALTER DEFAULT PRIVILEGES grants UPDATE on
  // every future table, so revoke it here; there is also no UPDATE policy.
  await sql`grant select, insert, delete on operator_sessions to osds_app`.execute(
    db,
  );
  await sql`revoke update on operator_sessions from osds_app`.execute(db);
  await sql`alter table operator_sessions enable row level security`.execute(db);
  await sql`alter table operator_sessions force row level security`.execute(db);

  await sql`
    create policy operator_sessions_self on operator_sessions
      for all
        using (
          token_hash = osds_current_session_token_hash()
          or operator_id = osds_current_operator_id()
        )
        with check (operator_id = osds_current_operator_id())
  `.execute(db);
  await sql`
    create policy operator_sessions_tenant_revoke on operator_sessions
      for delete using (
        osds_current_operator_id() is not null
        and operator_id in (
          select operator_id from staff_memberships
          where tenant_id = osds_current_tenant_id()
        )
      )
  `.execute(db);
}
