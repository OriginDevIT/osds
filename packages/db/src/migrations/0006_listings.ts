/**
 * 0006_listings - the core listing record (spec §4.1).
 *
 * Flattened location; generated `geog` (geography(Point,4326), GiST) for radius
 * and bbox queries; generated `search_tsv` (tsvector, GIN) over name +
 * description; pg_trgm GIN on `name` for typo tolerance (§12.1). JSONB holds
 * the open-ended contact.social array, external_profiles, attributes, media
 * and provenance. `tier` is nullable - NULL resolves to the tenant's rank-0
 * tier at read time; core sets it explicitly on `listing.tier_changed`
 * (§6; there is no listing.setTier command).
 *
 * Rollback:
 *   drop table if exists listings cascade;
 *   (roll back 0007 / 0009 / 0010 / 0011 first - they carry composite FKs to
 *   (tenant_id, id)). Forward-only: no down().
 */
import { sql } from "kysely";
import type { MigrationDb } from "./types.js";
import { enableTenantRls, touchUpdatedAt } from "./helpers.js";

export async function up(db: MigrationDb): Promise<void> {
  await sql`
    create table listings (
      id                 text primary key check (starts_with(id, 'listing_')),
      tenant_id          text not null references tenants (id) on delete cascade,
      slug               text not null,
      name               text not null,
      description        text,
      status             text not null default 'unclaimed'
                           check (status in ('unclaimed', 'claimed', 'suspended')),
      visibility         text not null default 'draft'
                           check (visibility in ('draft', 'published', 'hidden')),
      tier               text,

      address_line1      text,
      address_line2      text,
      locality           text,
      region             text,
      postal_code        text,
      country            text check (country is null or country ~ '^[A-Z]{2}$'),
      lat                double precision,
      lon                double precision,
      geo_precision      text not null default 'none'
                           check (geo_precision in ('rooftop', 'street', 'locality', 'none')),

      contact_phone_e164 text check (contact_phone_e164 is null
                           or contact_phone_e164 ~ '^[+][1-9][0-9]{1,14}$'),
      contact_email      text check (contact_email is null or contact_email = lower(contact_email)),
      contact_website    text,
      contact_social     jsonb not null default '[]',
      external_profiles  jsonb not null default '{}',
      attributes         jsonb not null default '{}',
      media              jsonb not null default '{}',
      reviews_disabled   boolean not null default false,
      provenance         jsonb,

      geog geography(Point, 4326) generated always as (
        case
          when lat is not null and lon is not null
          then st_setsrid(st_makepoint(lon, lat), 4326)::geography
        end
      ) stored,

      search_tsv tsvector generated always as (
        setweight(to_tsvector('simple', coalesce(name, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(description, '')), 'B')
      ) stored,

      created_at         timestamptz not null default now(),
      updated_at         timestamptz not null default now(),

      unique (tenant_id, id),
      unique (tenant_id, slug),
      foreign key (tenant_id, tier) references tiers (tenant_id, key)
    )
  `.execute(db);

  await sql`create index listings_search_tsv on listings using gin (search_tsv)`.execute(db);
  await sql`create index listings_geog on listings using gist (geog)`.execute(db);
  await sql`create index listings_name_trgm on listings using gin (name gin_trgm_ops)`.execute(db);
  await sql`create index listings_tenant_status on listings (tenant_id, status, visibility)`.execute(
    db,
  );

  await enableTenantRls(db, "listings");
  await touchUpdatedAt(db, "listings");
}
