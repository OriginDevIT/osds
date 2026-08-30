/**
 * dev seed - one fully worked directory for local development and DB-backed tests.
 *
 *   pnpm --filter @osds/db seed      # after `pnpm --filter @osds/db migrate`
 *
 * Shape (spec §4.1, §4.2, §6.1-§6.6):
 *   - one tenant, slug `chicago-plumbers`, mode `single`
 *   - three tiers: free (rank 0, not purchasable, no slot), verified (rank 1,
 *     purchasable, no slot), featured (rank 2, purchasable, uses a slot)
 *   - six categories; several listings sit in two, so `listing_categories` is
 *     exercised with multi-row membership
 *   - twenty listings across four real Chicago localities with real lat/lon, so
 *     the generated `geog` column and `search_tsv` have something to match;
 *     status, visibility and provenance.source are all varied
 *   - seven entitlements, one per row of the §6.5 rendering table: active,
 *     trialing, past_due, grace, expired, canceled, comped. The other thirteen
 *     listings have no entitlement and resolve to the rank-0 tier at read time
 *   - one featured slot_pool scoped category x locality, capacity 3, two slots
 *     occupied (by the `active` and `past_due` entitlements, the two §6.5 states
 *     that keep featured placement), the third slot available
 *
 * Idempotent: every id is a fixed, deterministic ULID and every write is an
 * upsert keyed on the primary key, so re-running converges on exactly the same
 * rows and never duplicates. Re-running rewrites each seeded column to the same
 * value; only the housekeeping `updated_at` advances (the BEFORE UPDATE
 * trigger fires on the upsert's DO UPDATE).
 *
 * Writes nothing to `outbox`. Seeding is not a state change anyone subscribes
 * to, and there is no worker yet.
 *
 * Connects via DATABASE_URL_ADMIN (falling back to DATABASE_URL). That role
 * bypasses the forced row-level security on every tenant table when it is a
 * superuser or holds BYPASSRLS (the bundled dev `osds` role is a superuser).
 * The transaction still sets `app.tenant_id`, which is a no-op in that case but
 * load-bearing when DATABASE_URL_ADMIN is a plain non-superuser table owner -
 * FORCE ROW LEVEL SECURITY then applies to it too.
 */
import { createHash } from "node:crypto";
import * as path from "node:path";
import { loadEnvFile } from "node:process";
import { sql } from "kysely";
import type { Kysely } from "kysely";
import { createKysely } from "./index.js";

// packages/db/src (or dist) -> repo root, anchored to this file (not the cwd,
// because `pnpm --filter` runs with the package as the working directory).
try {
  loadEnvFile(path.resolve(import.meta.dirname, "../../../.env"));
} catch {
  // No repo-root .env - rely on the ambient environment.
}

const connectionString = process.env.DATABASE_URL_ADMIN ?? process.env.DATABASE_URL;
if (!connectionString) {
  throw new Error("DATABASE_URL_ADMIN (or DATABASE_URL) is not set");
}

// --- deterministic ids --------------------------------------------------------

const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/**
 * A fixed, valid 26-char Crockford-base32 ULID derived from a label. Not
 * time-ordered - seed rows are never sorted by id - but stable: the same label
 * always yields the same id, which is what makes re-running the seed a no-op.
 * The body is 25 bytes of the label's SHA-256 mapped into the alphabet, so
 * distinct labels never collide; the leading `0` keeps the value inside the
 * 48-bit ULID timestamp ceiling.
 */
const ulid = (label: string): string => {
  const h = createHash("sha256").update(label).digest();
  let out = "0";
  for (let i = 0; i < 25; i++) out += CROCKFORD[h[i]! % 32];
  return out;
};

const TENANT_ID = `tnt_${ulid("tenant chicago plumbers")}`;
const TENANT_SLUG = "chicago-plumbers";
const USR_ADMIN = `usr_${ulid("usr admin")}`;
const USR_OWNER_1 = `usr_${ulid("usr owner one")}`;
const USR_OWNER_2 = `usr_${ulid("usr owner two")}`;
const POOL_ID = `pool_${ulid("pool featured lakeview plumbers")}`;
const CSV_BATCH_ID = `imp_${ulid("csv batch twentytwentysix q3")}`;

const catId = (slug: string): string => `cat_${ulid(`cat ${slug}`)}`;
const listingId = (n: number): string => `listing_${ulid(`listing number ${n}`)}`;
const entId = (key: string): string => `ent_${ulid(`entitlement ${key}`)}`;
const slotId = (n: number): string => `slot_${ulid(`slot number ${n}`)}`;

// --- reference data ----------------------------------------------------------

const CATEGORIES: ReadonlyArray<readonly [slug: string, name: string]> = [
  ["plumbers", "Plumbers"],
  ["emergency-plumbers", "Emergency Plumbers"],
  ["drain-cleaning", "Drain Cleaning"],
  ["water-heater-installation", "Water Heater Installation"],
  ["sewer-repair", "Sewer & Line Repair"],
  ["bathroom-remodeling", "Bathroom Remodeling"],
];

type LocalityName = "Lakeview" | "Logan Square" | "Hyde Park" | "Rogers Park";

const LOCALITY: Record<
  LocalityName,
  { lat: number; lon: number; postal: string; street: string }
> = {
  Lakeview: { lat: 41.9403, lon: -87.6438, postal: "60657", street: "W Belmont Ave" },
  "Logan Square": { lat: 41.9289, lon: -87.7076, postal: "60647", street: "N Milwaukee Ave" },
  "Hyde Park": { lat: 41.7943, lon: -87.5907, postal: "60615", street: "E 53rd St" },
  "Rogers Park": { lat: 42.01, lon: -87.669, postal: "60626", street: "N Clark St" },
};

type Provenance = "manual" | "csv_import" | "owner_submission" | "api";

interface ListingSpec {
  n: number;
  name: string;
  locality: LocalityName;
  status: "unclaimed" | "claimed" | "suspended";
  visibility: "draft" | "published" | "hidden";
  source: Provenance;
  tier: "free" | "verified" | "featured" | null;
  cats: string[];
}

// 20 listings, 5 per locality. status / visibility / source / tier all vary.
const LISTINGS: ListingSpec[] = [
  { n: 1, name: "Belmont Ave Plumbing", locality: "Lakeview", status: "claimed", visibility: "published", source: "owner_submission", tier: "featured", cats: ["plumbers", "emergency-plumbers"] },
  { n: 2, name: "Southport Rapid Drain", locality: "Lakeview", status: "claimed", visibility: "published", source: "owner_submission", tier: "featured", cats: ["plumbers", "drain-cleaning"] },
  { n: 3, name: "Wrigleyville Water Heaters", locality: "Lakeview", status: "claimed", visibility: "published", source: "manual", tier: "verified", cats: ["plumbers", "water-heater-installation"] },
  { n: 4, name: "Lincoln Ave Sewer & Rooter", locality: "Lakeview", status: "claimed", visibility: "published", source: "manual", tier: "verified", cats: ["plumbers", "sewer-repair"] },
  { n: 5, name: "Lakeview Emergency Plumbers", locality: "Lakeview", status: "claimed", visibility: "published", source: "owner_submission", tier: "verified", cats: ["plumbers", "emergency-plumbers"] },
  { n: 6, name: "Milwaukee Ave Plumbing Co", locality: "Logan Square", status: "claimed", visibility: "published", source: "csv_import", tier: "free", cats: ["plumbers"] },
  { n: 7, name: "Kedzie Drain Experts", locality: "Logan Square", status: "unclaimed", visibility: "published", source: "csv_import", tier: "free", cats: ["plumbers", "drain-cleaning"] },
  { n: 8, name: "Logan Square Pipe Works", locality: "Logan Square", status: "unclaimed", visibility: "published", source: "csv_import", tier: null, cats: ["drain-cleaning"] },
  { n: 9, name: "Fullerton Rooter Service", locality: "Logan Square", status: "unclaimed", visibility: "draft", source: "csv_import", tier: null, cats: ["water-heater-installation"] },
  { n: 10, name: "Palmer Square Plumbing", locality: "Logan Square", status: "unclaimed", visibility: "published", source: "api", tier: null, cats: ["plumbers"] },
  { n: 11, name: "53rd Street Plumbing", locality: "Hyde Park", status: "claimed", visibility: "published", source: "owner_submission", tier: null, cats: ["plumbers", "bathroom-remodeling"] },
  { n: 12, name: "Hyde Park Water Heater Pros", locality: "Hyde Park", status: "unclaimed", visibility: "hidden", source: "csv_import", tier: null, cats: ["sewer-repair"] },
  { n: 13, name: "Kenwood Sewer Solutions", locality: "Hyde Park", status: "suspended", visibility: "hidden", source: "manual", tier: null, cats: ["plumbers"] },
  { n: 14, name: "University Ave Rooter", locality: "Hyde Park", status: "unclaimed", visibility: "published", source: "api", tier: null, cats: ["water-heater-installation"] },
  { n: 15, name: "Lake Park Emergency Plumbing", locality: "Hyde Park", status: "unclaimed", visibility: "draft", source: "csv_import", tier: null, cats: ["plumbers", "sewer-repair"] },
  { n: 16, name: "Howard Street Plumbing", locality: "Rogers Park", status: "claimed", visibility: "published", source: "manual", tier: null, cats: ["bathroom-remodeling", "plumbers"] },
  { n: 17, name: "Sheridan Rd Drain Care", locality: "Rogers Park", status: "unclaimed", visibility: "published", source: "csv_import", tier: null, cats: ["drain-cleaning"] },
  { n: 18, name: "Rogers Park Pipe & Rooter", locality: "Rogers Park", status: "suspended", visibility: "hidden", source: "csv_import", tier: null, cats: ["plumbers"] },
  { n: 19, name: "Clark Street Water Heaters", locality: "Rogers Park", status: "unclaimed", visibility: "published", source: "api", tier: null, cats: ["emergency-plumbers", "plumbers"] },
  { n: 20, name: "Devon Ave Plumbing", locality: "Rogers Park", status: "unclaimed", visibility: "published", source: "owner_submission", tier: null, cats: ["plumbers"] },
];

const BLURB = [
  "Drain cleaning, sewer rodding, and 24/7 emergency service.",
  "Water heater installation and repair, tankless conversions, and leak detection.",
  "Sewer line camera inspection, spot repairs, and full line replacement.",
  "Fixture installs, repiping, sump pumps, and bathroom remodels.",
];

const slugify = (s: string): string =>
  s
    .toLowerCase()
    .replace(/&/g, "and")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");

// Fixed timestamps (RFC 3339, UTC, ms). Absolute, never relative to now(), so
// the rows are identical on every run.
const TS = {
  tenant: "2026-01-01T00:00:00.000Z",
  pool: "2026-05-01T00:00:00.000Z",
  activeStart: "2026-06-01T00:00:00.000Z",
  activeEnd: "2026-10-01T00:00:00.000Z",
  pastDueStart: "2026-08-01T00:00:00.000Z",
  pastDueEnd: "2026-09-01T00:00:00.000Z",
  pastDueDunning: "2026-08-26T00:00:00.000Z",
  trialStart: "2026-08-25T00:00:00.000Z",
  trialEnd: "2026-09-08T00:00:00.000Z",
  compStart: "2026-01-10T00:00:00.000Z",
  cancelStart: "2026-06-15T00:00:00.000Z",
  cancelEnd: "2026-09-15T00:00:00.000Z",
  graceStart: "2026-04-01T00:00:00.000Z",
  graceEnd: "2026-08-01T00:00:00.000Z",
  graceDunning: "2026-07-18T00:00:00.000Z",
  graceEnds: "2026-09-20T00:00:00.000Z",
  expiredStart: "2026-03-01T00:00:00.000Z",
  expiredEnd: "2026-05-30T00:00:00.000Z",
} as const;

// --- generic upsert --------------------------------------------------------

type Row = Record<string, unknown>;

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- generated schema types are not wired up yet
type Db = Kysely<any>;

const lit = (v: unknown) => {
  if (v === null || v === undefined) return sql`null`;
  if (typeof v === "object") return sql`${JSON.stringify(v)}::jsonb`;
  return sql`${v}`;
};

/**
 * `INSERT ... VALUES (...) ON CONFLICT (<key>) DO UPDATE SET <every other
 * column> = EXCLUDED.<column>`, or `DO NOTHING` when every column is part of
 * the key. All rows must share the same set of columns.
 */
async function upsert(db: Db, table: string, rows: Row[], conflict: string[]): Promise<void> {
  if (rows.length === 0) return;
  const cols = Object.keys(rows[0]!);
  const updates = cols.filter((c) => !conflict.includes(c));
  const setClause =
    updates.length > 0
      ? sql`do update set ${sql.join(
          updates.map((c) => sql`${sql.ref(c)} = excluded.${sql.ref(c)}`),
        )}`
      : sql`do nothing`;

  await sql`
    insert into ${sql.table(table)} (${sql.join(cols.map((c) => sql.ref(c)))})
    values ${sql.join(rows.map((r) => sql`(${sql.join(cols.map((c) => lit(r[c])))})`))}
    on conflict (${sql.join(conflict.map((c) => sql.ref(c)))}) ${setClause}
  `.execute(db);
}

// --- row builders ----------------------------------------------------------

function tierRows(): Row[] {
  const t = (key: string, rank: number, purchasable: boolean, uses_slot: boolean): Row => ({
    tenant_id: TENANT_ID,
    key,
    rank,
    purchasable,
    uses_slot,
    created_at: TS.tenant,
  });
  return [
    t("free", 0, false, false),
    t("verified", 1, true, false),
    t("featured", 2, true, true),
  ];
}

function categoryRows(): Row[] {
  return CATEGORIES.map(([slug, name]) => ({
    id: catId(slug),
    tenant_id: TENANT_ID,
    slug,
    name,
    parent_id: null,
    created_at: TS.tenant,
  }));
}

function userRows(): Row[] {
  const u = (id: string, email: string, name: string, role: string): Row => ({
    id,
    tenant_id: TENANT_ID,
    email,
    name,
    role,
    created_at: TS.tenant,
  });
  return [
    u(USR_ADMIN, "admin@chicago-plumbers.example", "Ada Okafor", "admin"),
    u(USR_OWNER_1, "dana@belmont-ave-plumbing.example", "Dana Reyes", "owner"),
    u(USR_OWNER_2, "sam@southport-rapid-drain.example", "Sam Ellis", "owner"),
  ];
}

function listingRows(): Row[] {
  return LISTINGS.map((l) => {
    const base = LOCALITY[l.locality];
    const slug = slugify(l.name);
    const spread = (l.n % 5) - 2;
    const createdAt = `2026-07-${String(10 + l.n).padStart(2, "0")}T09:00:00.000Z`;
    return {
      id: listingId(l.n),
      tenant_id: TENANT_ID,
      slug,
      name: l.name,
      description: `Licensed and insured plumbers serving ${l.locality}, Chicago. ${BLURB[l.n % 4]}`,
      status: l.status,
      visibility: l.visibility,
      tier: l.tier,
      address_line1: `${1200 + l.n} ${base.street}`,
      address_line2: l.n % 7 === 0 ? "Suite 2" : null,
      locality: l.locality,
      region: "IL",
      postal_code: base.postal,
      country: "US",
      lat: Number((base.lat + spread * 0.0018).toFixed(6)),
      lon: Number((base.lon + spread * 0.0021).toFixed(6)),
      geo_precision: l.n % 3 === 0 ? "street" : "rooftop",
      contact_phone_e164: `+1312555${String(100 + l.n).padStart(4, "0")}`,
      contact_email: `office@${slug}.example`,
      contact_website: `https://${slug}.example`,
      contact_social:
        l.n % 4 === 0
          ? [{ platform: "facebook", url: `https://facebook.com/${slug}`, label: null }]
          : [],
      external_profiles:
        l.n % 5 === 0
          ? { google: { place_id: `ChIJSEED${l.n}ExamplePlaceId0`, map_url: null, review_url: null } }
          : {},
      attributes: {
        hours: {},
        payment_methods: ["card", "check"],
        service_area_radius_km: 20 + (l.n % 5) * 2,
      },
      media: { logo: null, cover: null, gallery: [] },
      reviews_disabled: l.n === 13,
      provenance: {
        source: l.source,
        import_batch_id: l.source === "csv_import" ? CSV_BATCH_ID : null,
        submitted_by: l.source === "owner_submission" ? USR_OWNER_1 : null,
        created_at: createdAt,
        notes: null,
      },
      created_at: createdAt,
    };
  });
}

function listingCategoryRows(): Row[] {
  const rows: Row[] = [];
  for (const l of LISTINGS) {
    for (const slug of l.cats) {
      rows.push({
        tenant_id: TENANT_ID,
        listing_id: listingId(l.n),
        category_id: catId(slug),
      });
    }
  }
  return rows;
}

function slotPoolRows(): Row[] {
  return [
    {
      id: POOL_ID,
      tenant_id: TENANT_ID,
      tier: "featured",
      scope_type: "category_location",
      scope_category: catId("plumbers"),
      scope_locality: "Lakeview",
      capacity: 3,
      locked: 0,
      // A default listing for unsold slots is optional (spec §6.6); left unset
      // here so a dev sees a genuinely empty third slot.
      default_listing_id: null,
      term_days: 30,
      created_at: TS.pool,
    },
  ];
}

/**
 * One entitlement per row of the §6.5 public-rendering table. `active` and
 * `past_due` sit on `featured` and hold a slot (both keep featured placement);
 * `grace` was on `featured` but the slot has been released, so `slot_id` is
 * null; the rest are on `verified`, which uses no slot.
 */
function entitlementRows(): Row[] {
  const blank = {
    term_days: null,
    started_at: null,
    current_period_end: null,
    trial_ends_at: null,
    dunning_started_at: null,
    grace_ends_at: null,
    slot_id: null,
    cancel_at_period_end: false,
    comp: null,
    payment_ref: null,
  };
  const e = (key: string, listing: number, over: Partial<Row> & { tier: string; status: string; billing_mode: string; started_at: string }): Row => ({
    id: entId(key),
    tenant_id: TENANT_ID,
    listing_id: listingId(listing),
    ...blank,
    ...over,
    created_at: over.started_at,
  });

  return [
    e("active", 1, {
      tier: "featured",
      status: "active",
      billing_mode: "recurring",
      started_at: TS.activeStart,
      current_period_end: TS.activeEnd,
      slot_id: slotId(1),
      payment_ref: { adapter: "stripe", external_id: "sub_seed_active" },
    }),
    e("past_due", 2, {
      tier: "featured",
      status: "past_due",
      billing_mode: "recurring",
      started_at: TS.pastDueStart,
      current_period_end: TS.pastDueEnd,
      dunning_started_at: TS.pastDueDunning,
      slot_id: slotId(2),
      payment_ref: { adapter: "stripe", external_id: "sub_seed_pastdue" },
    }),
    e("trialing", 3, {
      tier: "verified",
      status: "trialing",
      billing_mode: "recurring",
      started_at: TS.trialStart,
      current_period_end: TS.trialEnd,
      trial_ends_at: TS.trialEnd,
      payment_ref: { adapter: "stripe", external_id: "sub_seed_trial" },
    }),
    e("comped", 4, {
      tier: "verified",
      status: "comped",
      billing_mode: "comp",
      started_at: TS.compStart,
      comp: {
        granted_by: USR_ADMIN,
        reason: "Neighborhood chamber of commerce partner",
        expires_at: null,
      },
    }),
    e("canceled", 5, {
      tier: "verified",
      status: "canceled",
      billing_mode: "recurring",
      started_at: TS.cancelStart,
      current_period_end: TS.cancelEnd,
      cancel_at_period_end: true,
      payment_ref: { adapter: "stripe", external_id: "sub_seed_canceled" },
    }),
    e("grace", 6, {
      tier: "featured",
      status: "grace",
      billing_mode: "recurring",
      started_at: TS.graceStart,
      current_period_end: TS.graceEnd,
      dunning_started_at: TS.graceDunning,
      grace_ends_at: TS.graceEnds,
      payment_ref: { adapter: "stripe", external_id: "sub_seed_grace" },
    }),
    e("expired", 7, {
      tier: "verified",
      status: "expired",
      billing_mode: "term",
      term_days: 90,
      started_at: TS.expiredStart,
      current_period_end: TS.expiredEnd,
      payment_ref: { adapter: "paypal", external_id: "txn_seed_expired" },
    }),
  ];
}

/**
 * One row per unit of pool capacity. Slots 1 and 2 are occupied by the `active`
 * and `past_due` entitlements; slot 3 is available. The nullable columns are
 * kept consistent with `status` per the migration's *_shape CHECKs.
 */
function slotRows(): Row[] {
  const free = {
    hold_kind: null,
    listing_id: null,
    entitlement_id: null,
    held_by: null,
    held_until: null,
    occupied_at: null,
    ends_at: null,
  };
  return [
    {
      id: slotId(1),
      tenant_id: TENANT_ID,
      pool_id: POOL_ID,
      slot_no: 1,
      status: "occupied",
      ...free,
      listing_id: listingId(1),
      entitlement_id: entId("active"),
      occupied_at: TS.activeStart,
      ends_at: TS.activeEnd,
      created_at: TS.activeStart,
    },
    {
      id: slotId(2),
      tenant_id: TENANT_ID,
      pool_id: POOL_ID,
      slot_no: 2,
      status: "occupied",
      ...free,
      listing_id: listingId(2),
      entitlement_id: entId("past_due"),
      occupied_at: TS.pastDueStart,
      ends_at: TS.pastDueEnd,
      created_at: TS.pastDueStart,
    },
    {
      id: slotId(3),
      tenant_id: TENANT_ID,
      pool_id: POOL_ID,
      slot_no: 3,
      status: "available",
      ...free,
      created_at: TS.pool,
    },
  ];
}

// --- run -----------------------------------------------------------------

const db = createKysely(connectionString);

try {
  const listings = listingRows();
  const listingCategories = listingCategoryRows();
  const entitlements = entitlementRows();

  await db.transaction().execute(async (trx) => {
    // `tenants` has no RLS, so it is written first, outside any tenant scope.
    await upsert(trx, "tenants", [
      {
        id: TENANT_ID,
        slug: TENANT_SLUG,
        domain: "chicago-plumbers.example",
        name: "Chicago Plumbers",
        mode: "single",
        created_at: TS.tenant,
      },
    ], ["id"]);

    // Scope the rest of the transaction to this tenant. A no-op when the admin
    // role bypasses RLS (a superuser, or one with BYPASSRLS - the bundled dev
    // `osds` role is a superuser); load-bearing when DATABASE_URL_ADMIN is a
    // plain non-superuser table owner, because every tenant table has FORCE ROW
    // LEVEL SECURITY and an unset app.tenant_id makes the policy default-deny.
    await sql`select set_config('app.tenant_id', ${TENANT_ID}, true)`.execute(trx);

    await upsert(trx, "tiers", tierRows(), ["tenant_id", "key"]);
    await upsert(trx, "categories", categoryRows(), ["id"]);
    await upsert(trx, "users", userRows(), ["id"]);
    await upsert(trx, "listings", listings, ["id"]);
    await upsert(trx, "listing_categories", listingCategories, [
      "tenant_id",
      "listing_id",
      "category_id",
    ]);
    await upsert(trx, "slot_pools", slotPoolRows(), ["id"]);
    // entitlements before slots: slots.entitlement_id is an immediate FK.
    // entitlements.slot_id -> slots is DEFERRABLE, so the `active` / `past_due`
    // rows may name their slot before it exists; it is checked at COMMIT.
    await upsert(trx, "entitlements", entitlements, ["id"]);
    await upsert(trx, "slots", slotRows(), ["id"]);
  });

  console.log(
    `seeded ${TENANT_SLUG}: ` +
      `${CATEGORIES.length} categories, ` +
      `${listings.length} listings, ` +
      `${listingCategories.length} listing_categories, ` +
      `${entitlements.length} entitlements (${entitlements
        .map((r) => r["status"])
        .join(", ")}), ` +
      `1 slot_pool (capacity 3, 2 occupied)`,
  );
} finally {
  await db.destroy();
}
