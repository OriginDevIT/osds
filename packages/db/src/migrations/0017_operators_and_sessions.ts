/**
 * 0017_operators_and_sessions - deployment operators, tenant staff, and
 * operator login sessions (issue #69, roles per spec §4.4 / issue #72).
 * Migration and generated types only: no persist functions, no route
 * handlers, no scrypt code.
 *
 * Not yet run in any deployment when spec §4.4 landed, so this migration is
 * corrected in place rather than followed by an 0018 that re-shapes it.
 *
 * Principals (spec §4.4): `operators` holds the deployment operator and every
 * tenant's staff; `staff_memberships (operator_id, tenant_id, role, status)`
 * is the tenant relationship. `users` (0005) is a different principal -
 * listing owners and claimants - and is untouched. One operator may
 * administer many tenants, so `operators` and `operator_sessions` carry no
 * `tenant_id`; the invariant-3 clarification in CLAUDE.md groups them with
 * `tenants`. `staff_memberships` carries `tenant_id` because it *is* that
 * relationship.
 *
 * Two axes, not one ladder (spec §4.4):
 *   - `operators.is_superadmin` - installation scope. Create/suspend/delete a
 *     tenant, elevate another operator, delete an operator: things that happen
 *     outside any tenant, so no membership can authorise them. It grants NO
 *     *implicit* tenant access - spec §4.4 is explicit that a superadmin has
 *     no automatic reach into a directory's data. But "nothing prevents them"
 *     from adding themselves a membership is the actual rule: if
 *     `staff_memberships` write authorization were admin-of-tenant only, a
 *     superadmin holding no membership could never become admin of any tenant
 *     and could never reach tenant data at all. So the INSERT and UPDATE
 *     policies have a superadmin branch - but scoped to `operator_id =
 *     osds_current_operator_id()`: a superadmin may grant themselves a
 *     membership, not silently edit someone else's staff list. To touch
 *     another operator's row they self-grant `admin` first and act as admin,
 *     which leaves two rows and an event - the audit trail, not an obstacle.
 *   - `staff_memberships.role` - five ordered ranks, `admin` > `manager` >
 *     `editor` > `moderator` > `support`. Per-tenant; the same operator may be
 *     `admin` on one tenant and `support` on another. This migration checks
 *     `role = 'admin'` only (`osds_operator_admins_current_tenant()`); nothing
 *     here compares ranks, so no rank/ordinal column is added.
 *   - `staff_memberships.status` - `pending` or `active`, default `pending`.
 *     A membership on an operator who already exists starts `pending` and
 *     confers nothing until accepted - otherwise an admin of one tenant could
 *     silently attach a directory to another admin's account. Every policy
 *     that treats membership as *authorization* (not mere visibility) requires
 *     `status = 'active'`.
 *
 * Three request-scoped GUCs, each read by a helper that mirrors
 * `osds_current_tenant_id()` (0001) - `nullif(current_setting(x, true), '')`,
 * so an unset GUC is NULL and `col = NULL` makes every policy default-deny.
 * `osds_operator_admins_current_tenant()` and
 * `osds_current_operator_is_superadmin()` fold them into the two
 * write-authorization predicates.
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
 * surface, or minted by a superadmin (#72). So `app.login_email` is not an
 * authorization gate - whatever can set it could mint any operator - and the
 * write policies key on `osds_operator_admins_current_tenant()` /
 * `osds_current_operator_is_superadmin()`. Fixing only the `operators` INSERT
 * would be cosmetic: the grant of tenant access is the `staff_memberships`
 * row, so its WITH CHECK carries the same admin-or-superadmin predicate.
 *
 * The wizard's first operator still cannot be written by `osds_app` under
 * these policies, superadmin branch included: `osds_operator_admins_current_
 * tenant()` needs an existing active admin row, and `osds_current_operator_
 * is_superadmin()` needs an existing operator row with `is_superadmin = true`
 * to already resolve from `app.operator_id` - at the very first boot there is
 * no session and no row to resolve either from. It is owner-provisioned
 * (`DATABASE_URL_ADMIN`), consistent with the wizard already needing the
 * owner for pre-auth writes to `tenants` / config / secrets. Once that first
 * operator exists (and is a superadmin), they need nothing further
 * owner-provisioned: the superadmin branch on `staff_memberships_provision` /
 * `staff_memberships_write` lets them grant themselves a membership on any
 * tenant, including the first one.
 *
 * A `pending` membership on `staff_memberships`, at any role, is never
 * authorization by itself - `osds_operator_admins_current_tenant()` requires
 * `status = 'active'`. But `staff_memberships_write`'s WITH CHECK cannot
 * therefore be admin-or-superadmin only: an existing operator's own pending
 * invitation would then be permanently inert - nothing, including the
 * invitee, could ever write `status = 'active'` onto it, and "confers nothing
 * until accepted" (spec §4.4) requires that accepting be reachable.
 * `staff_memberships_write`'s WITH CHECK gets a third branch, `operator_id =
 * osds_current_operator_id()` (self), for exactly this - on the UPDATE policy
 * only, so it cannot also grant a membership on INSERT. WITH CHECK cannot
 * itself narrow that branch to
 * "may change status but not role": it sees only the proposed new row, with
 * no reference to the row's pre-image - there is no OLD/NEW pairing available
 * inside a policy expression the way there is inside a trigger, and a
 * self-referencing subquery to approximate one would rely on undocumented
 * same-command tuple visibility, not a supported contract. So the coarse gate
 * (self may attempt to write this row) is RLS, and the fine invariant (a
 * self-write may only move `pending` to `active`, role and tenant unchanged)
 * is `staff_memberships_guard_self_accept`, a BEFORE UPDATE trigger with real
 * OLD/NEW access - the same reason `osds_set_updated_at` (0001) is a trigger
 * and not a generated column. The trigger applies only when the actor is
 * relying on the self branch specifically (not also an active admin or a
 * superadmin, either of whom may change role freely - spec §4.4, "change
 * roles" is what `admin` grants); it fires before RLS's own WITH CHECK is
 * evaluated for the same statement, so a violation aborts before that check
 * even runs.
 *
 * operators - RLS forced.
 *   operators_read       SELECT your own row, or the row whose email is
 *                        app.login_email (the login lookup, before any operator
 *                        is known).
 *   operators_provision  INSERT when the acting operator is an ACTIVE admin of
 *                        app.tenant_id, OR is a superadmin (#72). The wizard's
 *                        first operator is owner-provisioned regardless.
 *                        WITH CHECK passing is not enough for `RETURNING` on
 *                        that INSERT: RLS also holds the returned row to
 *                        operators_read's SELECT policy, which an inserter's
 *                        own id never matches for a row that is not theirs.
 *                        A provisioning caller that wants the row back sets
 *                        app.login_email to the new operator's email first,
 *                        satisfying operators_read's other branch - verified
 *                        empirically (INSERT succeeds either way; INSERT ...
 *                        RETURNING only succeeds with that GUC set).
 *   operators_self_write UPDATE your own row (rehash-on-login raises the scrypt
 *                        parameters embedded in `password_hash`; also where a
 *                        password-set on invite-accept would land).
 *   No DELETE policy: operator removal is not a decided flow, so `osds_app`
 *   cannot delete an operator row yet.
 *
 * staff_memberships - RLS forced, and NOT via enableTenantRls: that shared
 *   `tenant_isolation` policy is granted to PUBLIC and would let a public page
 *   render read every tenant's staff list. Four per-command policies, because
 *   the wide SELECT net and the narrow write gates do not belong together (a
 *   FOR ALL WITH CHECK let the self branch grant a membership on INSERT, where
 *   no trigger fires):
 *   staff_memberships_read      SELECT, status-blind on purpose: your own row
 *     whatever its status (so a pending invitee can see and accept it), or -
 *     only while acting as an operator - every row of app.tenant_id, pending
 *     ones included (so admins see who has not responded).
 *   staff_memberships_provision INSERT: an ACTIVE `admin` of app.tenant_id, OR
 *     a superadmin creating THEIR OWN row. No bare-self branch - an operator
 *     cannot self-insert a membership; invitations are created by admins.
 *   staff_memberships_write     UPDATE: the same two branches, plus the acting
 *     operator on their own row (the invitee-accept path), which
 *     `staff_memberships_guard_self_accept` holds to a `pending` -> `active`
 *     status move, role and tenant unchanged.
 *   staff_memberships_purge     DELETE: an ACTIVE `admin` of app.tenant_id
 *     (removing staff), or the acting operator on their own row (leaving, or
 *     declining a pending invitation).
 *   staff_memberships_guard_self_accept  BEFORE UPDATE trigger enforcing the
 *     one invariant WITH CHECK cannot: on a self-write that is not also an
 *     admin or superadmin write, `old.status` must be `pending`, `new.status`
 *     `active`, and role and tenant unchanged. Everything else raises.
 *
 * operator_sessions - RLS forced. Rows are immutable: absolute `expires_at`, no
 *   sliding refresh, no UPDATE grant, no UPDATE policy.
 *   operator_sessions_self           the session whose token_hash the request
 *     presented (resolution, logout), or every session of the authenticated
 *     operator (session list, log-out-everywhere) - independent of any
 *     membership or its status, since a session is not tenant data.
 *   operator_sessions_tenant_revoke  DELETE only: `tenant.suspended` deletes
 *     every session of every operator with an ACTIVE membership in
 *     app.tenant_id in one statement, as `osds_app`, gated on there being an
 *     operator so a public render cannot trigger it. A `pending` membership
 *     never had tenant access, so it does not contribute to the sweep. The
 *     subquery reads `staff_memberships` under its own RLS, so it needs
 *     app.tenant_id set to the suspended tenant and an operator context - a
 *     scheduled/system path with neither is a known gap, reported separately,
 *     not fixed here.
 *
 * Rollback:
 *   drop policy if exists operator_sessions_tenant_revoke on operator_sessions;
 *   drop policy if exists operator_sessions_self on operator_sessions;
 *   drop trigger if exists staff_memberships_guard_self_accept on staff_memberships;
 *   drop function if exists osds_guard_staff_membership_self_accept();
 *   drop policy if exists staff_memberships_purge on staff_memberships;
 *   drop policy if exists staff_memberships_write on staff_memberships;
 *   drop policy if exists staff_memberships_provision on staff_memberships;
 *   drop policy if exists staff_memberships_read on staff_memberships;
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
 *   drop function if exists osds_current_operator_is_superadmin();
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
      is_superadmin  boolean not null default false,
      created_at     timestamptz not null default now(),
      updated_at     timestamptz not null default now()
    )
  `.execute(db);

  await sql`grant select, insert, update, delete on operators to osds_app`.execute(
    db,
  );
  await sql`alter table operators enable row level security`.execute(db);
  await sql`alter table operators force row level security`.execute(db);

  // True when the acting operator's own row has is_superadmin. Reads through
  // operators_read's id = osds_current_operator_id() branch, so an unset
  // app.operator_id (or one naming no row) yields no row and coalesces to
  // false - default-deny, same as every other resolver here.
  await sql`
    create function osds_current_operator_is_superadmin() returns boolean
      language sql
      stable
      as $$
        select coalesce(
          (select is_superadmin from operators where id = osds_current_operator_id()),
          false
        )
      $$
  `.execute(db);

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
      role         text not null
                     check (role in ('admin', 'manager', 'editor', 'moderator', 'support')),
      status       text not null default 'pending' check (status in ('pending', 'active')),
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

  // True when the acting operator holds an ACTIVE `admin` membership for the
  // tenant in app.tenant_id - a `pending` admin invite grants nothing until
  // accepted (spec §4.4). Gates who may provision an operator or a membership.
  // NULL GUCs -> no match -> false, so a public render (app.tenant_id only, no
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
            and status = 'active'
        )
      $$
  `.execute(db);

  // Issue #72: a superadmin may mint an operator row without being an admin of
  // any tenant (spec §4.4 - installation-scope powers happen outside any
  // tenant). This is unscoped on purpose: minting an operator row grants no
  // access - the staff_memberships row does, and that policy scopes its
  // superadmin branch to the superadmin's own row.
  await sql`
    create policy operators_provision on operators
      for insert with check (
        osds_operator_admins_current_tenant()
        or osds_current_operator_is_superadmin()
      )
  `.execute(db);

  // Per-command, not one FOR ALL policy: SELECT wants a wide net, the write
  // paths do not, and folding them together let the self branch grant a
  // membership on INSERT (no trigger fires there) and let any operator acting
  // in a tenant delete that tenant's staff.
  //
  //   read      SELECT, deliberately status-blind: your own row whatever its
  //             status (so a pending invitee can see and accept it), or - only
  //             while acting as an operator - every row of app.tenant_id,
  //             pending ones included (so admins see who has not responded).
  //   provision INSERT: an ACTIVE admin of app.tenant_id, OR a superadmin
  //             creating THEIR OWN membership row (spec §4.4 sanctions a
  //             superadmin self-granting; editing someone else's staff list is
  //             the admin's job - a superadmin who needs it self-grants admin
  //             first, leaving two rows, which is the audit trail).
  //   write     UPDATE: the same two branches, plus the acting operator on
  //             their own row - the invitee-accept path, held by
  //             staff_memberships_guard_self_accept below to a pending ->
  //             active status move with role and tenant unchanged. USING is
  //             the read net; WITH CHECK is the gate.
  //   purge     DELETE: an ACTIVE admin of app.tenant_id (removing staff), or
  //             the acting operator on their own row (leaving, or declining a
  //             pending invitation).
  await sql`
    create policy staff_memberships_read on staff_memberships
      for select using (
        operator_id = osds_current_operator_id()
        or (
          osds_current_operator_id() is not null
          and tenant_id = osds_current_tenant_id()
        )
      )
  `.execute(db);
  await sql`
    create policy staff_memberships_provision on staff_memberships
      for insert with check (
        osds_operator_admins_current_tenant()
        or (
          osds_current_operator_is_superadmin()
          and operator_id = osds_current_operator_id()
        )
      )
  `.execute(db);
  await sql`
    create policy staff_memberships_write on staff_memberships
      for update
        using (
          operator_id = osds_current_operator_id()
          or (
            osds_current_operator_id() is not null
            and tenant_id = osds_current_tenant_id()
          )
        )
        with check (
          osds_operator_admins_current_tenant()
          or (
            osds_current_operator_is_superadmin()
            and operator_id = osds_current_operator_id()
          )
          or operator_id = osds_current_operator_id()
        )
  `.execute(db);
  await sql`
    create policy staff_memberships_purge on staff_memberships
      for delete using (
        osds_operator_admins_current_tenant()
        or operator_id = osds_current_operator_id()
      )
  `.execute(db);

  // The fine invariant on the self branch of staff_memberships_write's WITH
  // CHECK. Fires only when the acting operator is this row's own operator and
  // is NOT relying on the admin or superadmin branch (either of which may
  // change role freely - spec §4.4). In that case the only permitted change is
  // status pending -> active; role and tenant must be unchanged. Anything else
  // aborts the statement. BEFORE UPDATE, so it runs before RLS evaluates its
  // own WITH CHECK for the same row.
  await sql`
    create function osds_guard_staff_membership_self_accept() returns trigger
      language plpgsql
      as $$
      begin
        if new.operator_id = osds_current_operator_id()
           and not osds_operator_admins_current_tenant()
           and not osds_current_operator_is_superadmin()
        then
          if old.status <> 'pending'
             or new.status <> 'active'
             or new.role is distinct from old.role
             or new.tenant_id is distinct from old.tenant_id
             or new.operator_id is distinct from old.operator_id
          then
            raise exception 'staff_memberships: a self write may only move status from pending to active, role and tenant unchanged';
          end if;
        end if;
        return new;
      end
      $$
  `.execute(db);
  await sql`
    create trigger staff_memberships_guard_self_accept
      before update on staff_memberships
      for each row execute function osds_guard_staff_membership_self_accept()
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
            and status = 'active'
        )
      )
  `.execute(db);
}
