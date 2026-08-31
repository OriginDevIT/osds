/**
 * Persistence for the `listing.upsert` command - spec §7 (commands), §11.1
 * (Postgres outbox).
 *
 * This is the one place the pure {@link handleListingUpsert} resolver meets the
 * database. It pulls in kysely and pg, so it lives under `@osds/core/persist`
 * and is never re-exported from the package root - `packages/web` imports the
 * resolvers from `@osds/core` and must not drag a driver into the Next bundle
 * (issue #26).
 *
 * One transaction, connected as `osds_app` (never the table owner) with the
 * tenant GUC set:
 *
 *   1. look up `idempotency_key` before any work; a replay returns the original
 *      event id so the caller can answer 409, and nothing is re-emitted;
 *   2. load the current listing, tenant-scoped, by the §7 matching key
 *      (payload `id`, else `slug`);
 *   3. run {@link handleListingUpsert};
 *   4. `rejected`  -> return the problem, write nothing;
 *      `unchanged` -> return, write nothing (no row update, no outbox row);
 *      `created` / `updated` -> apply the row change and insert the outbox row,
 *      together or not at all (§11.1).
 *
 * `deps` ({@link PersistDeps}) supplies the clock and the id factory - injected,
 * never imported, so the resolver stays pure and this layer stays testable.
 *
 * On the `updated` branch every JSON Patch op must map to a `listings` column
 * SET: an op whose path is not in {@link COLUMN_FOR_PATH}, an op that is not a
 * `replace`, or an empty assignment list is a bug and throws rather than
 * emitting a `listing.updated` event the row change does not back (design rule
 * 2). Category membership (`listing_categories`) is not written yet, so
 * `handleListingUpsert` rejects the `categories` field outright (issue #42) -
 * no `/categories` op ever reaches here. Claim command persistence follows
 * separately.
 */
import { sql } from "@osds/db";
import type { Kysely } from "@osds/db";
import type { OsdsCommand, ProblemDocument } from "@osds/adapter-kit";
import {
  handleListingUpsert,
  withSubject,
  type CreatedListing,
  type GeoPrecision,
  type Listing,
} from "../command/listing-upsert.js";
import type { JsonPatchOp } from "../command/json-patch.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- generated schema types are not wired up yet
type Db = Kysely<any>;

/** Injected effects. Never imported here - the pure resolver must stay pure. */
export interface PersistDeps {
  /** Wall clock, for the event's `occurred_at`. */
  readonly now: () => Date;
  /** ULID factory. The `listing_` prefix for a new listing id is added here. */
  readonly newId: () => string;
}

export type PersistListingUpsertResult =
  | { readonly status: "created"; readonly event_id: string }
  | { readonly status: "updated"; readonly event_id: string }
  | { readonly status: "unchanged" }
  | { readonly status: "duplicate"; readonly event_id: string }
  | { readonly status: "rejected"; readonly problem: ProblemDocument };

/** JSON Pointer -> `listings` column, for the paths {@link handleListingUpsert} emits on an update. */
const COLUMN_FOR_PATH: Readonly<Record<string, string>> = {
  "/slug": "slug",
  "/name": "name",
  "/description": "description",
  "/location/address_line1": "address_line1",
  "/location/address_line2": "address_line2",
  "/location/locality": "locality",
  "/location/region": "region",
  "/location/postal_code": "postal_code",
  "/location/country": "country",
  "/location/lat": "lat",
  "/location/lon": "lon",
  "/location/geo_precision": "geo_precision",
  "/contact/phone_e164": "contact_phone_e164",
  "/contact/email": "contact_email",
  "/contact/website": "contact_website",
};

export async function persistListingUpsert(
  db: Db,
  command: OsdsCommand,
  deps: PersistDeps,
): Promise<PersistListingUpsertResult> {
  try {
    return await withTenant(db, command.tenant_id, (trx) =>
      applyInTransaction(trx, command, deps),
    );
  } catch (err) {
    // A concurrent replay of the same command won the race on the partial
    // unique index (outbox_idempotency). Re-read and return the winner's id.
    if (isUniqueViolation(err)) {
      const original = await withTenant(db, command.tenant_id, (trx) =>
        findEventId(trx, command.tenant_id, command.idempotency_key),
      );
      if (original !== null) return { status: "duplicate", event_id: original };
    }
    throw err;
  }
}

// --- transaction body -------------------------------------------------

async function applyInTransaction(
  trx: Db,
  command: OsdsCommand,
  deps: PersistDeps,
): Promise<PersistListingUpsertResult> {
  const replayId = await findEventId(
    trx,
    command.tenant_id,
    command.idempotency_key,
  );
  if (replayId !== null) return { status: "duplicate", event_id: replayId };

  const current = await loadListing(trx, command);
  const result = handleListingUpsert(command, current);

  if (result.outcome === "rejected") {
    return { status: "rejected", problem: result.problem };
  }
  if (result.outcome === "unchanged") {
    return { status: "unchanged" };
  }

  if (result.outcome === "created") {
    const listingId = `listing_${deps.newId()}`;
    const event = withSubject(result.event, listingId);
    await insertListing(trx, command.tenant_id, listingId, event.data.listing);
    const eventId = await insertOutbox(trx, command, deps, {
      type: "listing.created",
      subject: listingId,
      data: event.data,
    });
    return { status: "created", event_id: eventId };
  }

  await applyListingUpdate(
    trx,
    command.tenant_id,
    result.event.subject,
    result.event.data.changes,
  );
  const eventId = await insertOutbox(trx, command, deps, {
    type: "listing.updated",
    subject: result.event.subject,
    data: result.event.data,
  });
  return { status: "updated", event_id: eventId };
}

// --- reads ----------------------------------------------------------

interface ListingRow {
  readonly id: string;
  readonly tenant_id: string;
  readonly slug: string;
  readonly name: string;
  readonly description: string | null;
  readonly address_line1: string | null;
  readonly address_line2: string | null;
  readonly locality: string | null;
  readonly region: string | null;
  readonly postal_code: string | null;
  readonly country: string | null;
  readonly lat: number | null;
  readonly lon: number | null;
  readonly geo_precision: GeoPrecision;
  readonly contact_phone_e164: string | null;
  readonly contact_email: string | null;
  readonly contact_website: string | null;
}

const SELECT_LISTING = sql`
  select id, tenant_id, slug, name, description,
         address_line1, address_line2, locality, region, postal_code, country,
         lat, lon, geo_precision,
         contact_phone_e164, contact_email, contact_website
  from listings
`;

async function loadListing(
  trx: Db,
  command: OsdsCommand,
): Promise<Listing | null> {
  const { id, slug } = matchKeys(command);

  let rows: readonly ListingRow[];
  if (id !== undefined) {
    rows = (
      await sql<ListingRow>`${SELECT_LISTING} where tenant_id = ${command.tenant_id} and id = ${id} limit 1`.execute(
        trx,
      )
    ).rows;
  } else if (slug !== undefined) {
    rows = (
      await sql<ListingRow>`${SELECT_LISTING} where tenant_id = ${command.tenant_id} and slug = ${slug} limit 1`.execute(
        trx,
      )
    ).rows;
  } else {
    // Neither key present: let the resolver reject the malformed payload.
    return null;
  }

  const row = rows[0];
  if (row === undefined) return null;

  return {
    id: row.id,
    tenant_id: row.tenant_id,
    slug: row.slug,
    name: row.name,
    description: row.description,
    location: {
      address_line1: row.address_line1,
      address_line2: row.address_line2,
      locality: row.locality,
      region: row.region,
      postal_code: row.postal_code,
      country: row.country,
      lat: row.lat,
      lon: row.lon,
      geo_precision: row.geo_precision,
    },
    contact: {
      phone_e164: row.contact_phone_e164,
      email: row.contact_email,
      website: row.contact_website,
    },
  };
}

/** The §7 matching key straight off the payload - the resolver owns validation. */
function matchKeys(command: OsdsCommand): {
  id?: string;
  slug?: string;
} {
  const p = command.payload as Record<string, unknown>;
  const id = typeof p["id"] === "string" && p["id"] ? p["id"] : undefined;
  const slug =
    typeof p["slug"] === "string" && p["slug"] ? p["slug"] : undefined;
  return { ...(id ? { id } : {}), ...(slug ? { slug } : {}) };
}

async function findEventId(
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

// --- writes ---------------------------------------------------------

async function insertListing(
  trx: Db,
  tenantId: string,
  listingId: string,
  listing: CreatedListing,
): Promise<void> {
  await sql`
    insert into listings (
      id, tenant_id, slug, name, description,
      address_line1, address_line2, locality, region, postal_code, country,
      lat, lon, geo_precision,
      contact_phone_e164, contact_email, contact_website
    ) values (
      ${listingId}, ${tenantId}, ${listing.slug}, ${listing.name}, ${listing.description},
      ${listing.location.address_line1}, ${listing.location.address_line2},
      ${listing.location.locality}, ${listing.location.region},
      ${listing.location.postal_code}, ${listing.location.country},
      ${listing.location.lat}, ${listing.location.lon}, ${listing.location.geo_precision},
      ${listing.contact.phone_e164}, ${listing.contact.email}, ${listing.contact.website}
    )
  `.execute(trx);
}

/**
 * Apply an `updated` result's JSON Patch to the `listings` row. Exported for
 * the guard tests below; not part of the `@osds/core/persist` surface.
 */
export async function applyListingUpdate(
  trx: Db,
  tenantId: string,
  listingId: string,
  changes: readonly JsonPatchOp[],
): Promise<void> {
  // Every op must project to a column SET. A path we cannot map, or an op that
  // is not a `replace`, means the `listing.updated` event would assert a change
  // the row does not carry - fail loudly instead of dropping it (design rule 2).
  const assignments: unknown[] = [];
  for (const op of changes) {
    if (op.op !== "replace") {
      throw new Error(
        `listing.upsert persistence: unexpected JSON Patch op "${op.op}" at "${op.path}"`,
      );
    }
    const column = COLUMN_FOR_PATH[op.path];
    if (column === undefined) {
      throw new Error(
        `listing.upsert persistence: JSON Patch path "${op.path}" maps to no listings column`,
      );
    }
    assignments.push(sql`${sql.ref(column)} = ${op.value}`);
  }
  if (assignments.length === 0) {
    throw new Error(
      "listing.upsert persistence: an updated result produced no column assignments",
    );
  }

  // The listings_touch_updated_at trigger maintains updated_at.
  await sql`
    update listings set ${sql.join(assignments, sql`, `)}
    where tenant_id = ${tenantId} and id = ${listingId}
  `.execute(trx);
}

async function insertOutbox(
  trx: Db,
  command: OsdsCommand,
  deps: PersistDeps,
  event: {
    readonly type: "listing.created" | "listing.updated";
    readonly subject: string;
    readonly data: unknown;
  },
): Promise<string> {
  const id = deps.newId();
  const actor = JSON.stringify({ type: "adapter", id: command.adapter_id });

  await sql`
    insert into outbox (
      id, tenant_id, type, version, occurred_at, subject,
      actor, origin, trace_id, data, idempotency_key
    ) values (
      ${id}, ${command.tenant_id}, ${event.type}, 1, ${deps.now().toISOString()}, ${event.subject},
      ${actor}::jsonb, ${command.adapter_id}, ${command.trace_id},
      ${JSON.stringify(event.data)}::jsonb, ${command.idempotency_key}
    )
  `.execute(trx);

  return id;
}

// --- plumbing -----------------------------------------------------

/**
 * Run `fn` in one transaction as `osds_app` (so RLS is enforced - the role is
 * NOBYPASSRLS and not the table owner) with `app.tenant_id` set for its
 * duration. Both settings are transaction-local and reset on commit/rollback.
 */
function withTenant<T>(
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

function isUniqueViolation(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { code?: string }).code === "23505"
  );
}
