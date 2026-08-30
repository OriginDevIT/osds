# @osds/db

Postgres schema and access layer for OSDS: [Kysely](https://kysely.dev) query
builder, `pg` driver, hand-written SQL migrations. **The database is the source
of truth for the schema.**

## Migrations

`src/migrations/NNNN_name.ts` — each file runs raw SQL through Kysely's `sql`
tag and is tracked in `kysely_migration`. **Forward-only**: a merged migration
is never edited or reverted; every file's header carries a manual rollback note
for emergencies.

    pnpm --filter @osds/db migrate      # apply all pending
    pnpm migrate:dev                    # same, from the repo root

Migrations connect as the **table owner** via `DATABASE_URL_ADMIN` (falling
back to `DATABASE_URL`). Requires a Postgres with the `postgis` and `pg_trgm`
extensions available on disk. The bundled `postgis/postgis` image
(`docker-compose.yml`) has both; migration `0001` runs `CREATE EXTENSION`.

The runner applies the whole batch in one transaction — a failure rolls back
cleanly.

## Dev seed

    pnpm --filter @osds/db seed         # after `migrate`

`src/seed.ts` populates one worked directory (`chicago-plumbers`, mode
`single`) for local development and DB-backed tests: three tiers, six
categories, twenty listings across four real Chicago localities with real
lat/lon, and seven entitlements — one per row of the spec §6.5 rendering table
(`active`, `trialing`, `past_due`, `grace`, `expired`, `canceled`, `comped`) —
plus one `featured` slot pool (capacity 3, two slots occupied). It connects as
the **table owner** via `DATABASE_URL_ADMIN` so forced RLS is not in the way.

Every id is a fixed, deterministic ULID and every write is an upsert, so the
seed is **idempotent** — re-running converges on the same rows and never
duplicates (only `updated_at` advances). It writes nothing to `outbox`.

## Generated types

    pnpm --filter @osds/db codegen      # writes src/schema.ts from a migrated DB

`src/schema.ts` is generated and git-ignored. Once it exists, pass its `DB`
type to the entry point:

    import { createKysely } from "@osds/db";
    import type { DB } from "@osds/db/schema";
    const db = createKysely<DB>();

## Row-level security

Every table except `tenants` has **forced** RLS scoping rows to
`current_setting('app.tenant_id')`. RLS is only enforced against a role that is
neither the table owner nor holds `BYPASSRLS`.

- **`osds_app`** is that role. Migration `0013` creates it (`NOLOGIN`,
  `NOSUPERUSER`, `NOBYPASSRLS`) and grants it `SELECT/INSERT/UPDATE/DELETE` on
  the tenant tables (plus read-only `spatial_ref_sys`) and nothing more. The
  app and worker connect as it via
  `DATABASE_URL`, and must `SET app.tenant_id` (or `SET LOCAL` per transaction)
  before any query — with the var unset, every policy returns zero rows, and a
  cross-tenant write is refused by the policy's `WITH CHECK`.
- Granting `osds_app` a **login and password** is a deployment step (it touches
  authentication — see `docs/agent-operations.md`). For local dev,
  `infra/postgres/init/10-osds-app-role.sql` does it on first cluster init; if
  your `pgdata` volume predates this, create the login by hand once or run
  `pnpm infra:reset`.
- Migrations and codegen connect as the **owner** via `DATABASE_URL_ADMIN`.
- The outbox consumer is the one cross-tenant component: it runs as a role with
  `BYPASSRLS`, or iterates tenants and sets `app.tenant_id` per batch.

## Scope

Tables so far: `tenants`, `tiers`, `categories`, `listing_categories`, `users`,
`listings`, `claims`, `entitlements`, `slot_pools`, `slots`, `outbox`. Reviews,
leads, moderation, compliance, agent, import and postal tables are not modelled
yet.
