/**
 * 0005_users - real accounts (owner / staff / admin). Distinct from event
 * `actor.type`, which also covers visitor / system / agent / adapter.
 *
 * Emails are stored lower-cased; a CHECK enforces it. `unique (tenant_id, id)`
 * backs the claims FKs.
 *
 * Rollback:
 *   drop table if exists users cascade;
 *   (roll back 0008 first). Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types";
import { enableTenantRls, touchUpdatedAt } from "./helpers";

export async function up(db: MigrationDb): Promise<void> {
  await sql`
    create table users (
      id          text primary key check (starts_with(id, 'usr_')),
      tenant_id   text not null references tenants (id) on delete cascade,
      email       text not null check (email = lower(email)),
      name        text,
      role        text not null check (role in ('owner', 'staff', 'admin')),
      created_at  timestamptz not null default now(),
      updated_at  timestamptz not null default now(),
      unique (tenant_id, id),
      unique (tenant_id, email)
    )
  `.execute(db);

  await enableTenantRls(db, "users");
  await touchUpdatedAt(db, "users");
}
