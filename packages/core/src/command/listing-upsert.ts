/**
 * The `listing.upsert` command handler - spec §7 (commands), §4.1 (listing
 * entity), §2 (envelope).
 *
 * Pure, like {@link ../entitlement.ts}: no database, no clock, no id factory, no
 * I/O. The caller looks the listing up and passes it in (or `null`); this module
 * validates the command and decides which event to emit.
 *
 *   - No current listing  -> `listing.created` as a draft with no `subject`.
 *     The listing id does not exist yet; the caller mints it and calls
 *     {@link withSubject} to get an emittable event (§2). The handler never
 *     returns a create with a null subject.
 *   - An existing listing the payload changes -> `listing.updated`, carrying
 *     `changes` as an RFC 6902 JSON Patch from stored to desired content (§3.3).
 *   - An existing listing the payload does not change -> `unchanged`, no event.
 *     A `listing.updated` carrying an empty patch is not a fact (design rule 2),
 *     so the decision is made here, not left to the caller.
 *
 * The payload is treated as a partial desired state: fields it omits are left
 * untouched on update and take their defaults on create. Fields it sets to
 * `null` are cleared.
 *
 * Two fields are refused outright (§7): `tier` is derived from entitlement
 * (§6, there is no `listing.setTier`), and `status` moves through the claim
 * flow (§9). Envelope assembly - `id`, `occurred_at`, `actor`, `origin` - is
 * the persistence layer's job; idempotency and the 409 replay path land there
 * too and are out of scope here.
 *
 * Matching rule (§7): when the payload carries an `id`, that is the identity;
 * otherwise it is `(tenant_id, slug)`. No other matching rule exists. The caller
 * is trusted to pass the listing that rule selects, or `null`.
 */
import type { OsdsCommand, ProblemDocument } from "@osds/adapter-kit";
import { validationProblem } from "./problem.js";
import { jsonPatch, type JsonPatchOp } from "./json-patch.js";

// --- the listing entity (the slice this command reads/writes) -------------

export type GeoPrecision = "rooftop" | "street" | "locality" | "none";

export interface ListingLocation {
  readonly address_line1: string | null;
  readonly address_line2: string | null;
  readonly locality: string | null;
  readonly region: string | null;
  readonly postal_code: string | null;
  /** ISO 3166-1 alpha-2, uppercase. */
  readonly country: string | null;
  readonly lat: number | null;
  readonly lon: number | null;
  readonly geo_precision: GeoPrecision;
}

export interface ListingContact {
  /** E.164. */
  readonly phone_e164: string | null;
  /** Lowercased. */
  readonly email: string | null;
  readonly website: string | null;
}

/** The mutable content of a listing - what `listing.updated` diffs over. */
export interface ListingContent {
  readonly slug: string;
  readonly name: string;
  readonly description: string | null;
  readonly categories: readonly string[];
  readonly location: ListingLocation;
  readonly contact: ListingContact;
}

/** A stored listing, as handed to {@link handleListingUpsert}. */
export interface Listing extends ListingContent {
  readonly id: string;
  readonly tenant_id: string;
}

/** The listing carried on `listing.created`. `id` is null until persistence mints one. */
export interface CreatedListing extends ListingContent {
  readonly id: string | null;
  readonly tenant_id: string;
}

// --- result --------------------------------------------------------------

export type ListingMatch =
  | { readonly by: "id"; readonly id: string }
  | { readonly by: "slug"; readonly tenant_id: string; readonly slug: string };

/** `listing.created` once its `subject` (the minted listing id) is attached. */
export interface ListingCreatedEvent {
  readonly type: "listing.created";
  readonly subject: string;
  readonly data: { readonly listing: CreatedListing };
}

/**
 * A {@link ListingCreatedEvent} with no `subject` - the only create shape the
 * handler returns. The id is minted downstream; {@link withSubject} turns the
 * draft into an emittable event, so a null-subject create is unrepresentable.
 */
export type ListingCreatedDraft = Omit<ListingCreatedEvent, "subject">;

export interface ListingUpdatedEvent {
  readonly type: "listing.updated";
  readonly subject: string;
  readonly data: { readonly changes: readonly JsonPatchOp[] };
}

export type ListingUpsertResult =
  | { readonly outcome: "rejected"; readonly problem: ProblemDocument }
  | { readonly outcome: "unchanged"; readonly match: ListingMatch }
  | {
      readonly outcome: "created";
      readonly match: ListingMatch;
      readonly event: ListingCreatedDraft;
    }
  | {
      readonly outcome: "updated";
      readonly match: ListingMatch;
      readonly event: ListingUpdatedEvent;
    };

/**
 * Attach the minted listing id to a create draft as its event `subject`. The
 * subject of a `listing.created` is by definition the new listing's id, so the
 * same id also lands on `data.listing.id`.
 */
export function withSubject(
  draft: ListingCreatedDraft,
  id: string,
): ListingCreatedEvent {
  return {
    type: draft.type,
    subject: id,
    data: { listing: { ...draft.data.listing, id } },
  };
}

// --- constants ---------------------------------------------------------

const COMMAND_NAME = "listing.upsert";
const KEY_SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const E164 = /^\+[1-9]\d{1,14}$/;
const ALPHA2 = /^[A-Z]{2}$/;
const GEO_PRECISIONS: ReadonlySet<string> = new Set([
  "rooftop",
  "street",
  "locality",
  "none",
]);
const LOCATION_KEYS: ReadonlySet<string> = new Set([
  "address_line1",
  "address_line2",
  "locality",
  "region",
  "postal_code",
  "country",
  "lat",
  "lon",
  "geo_precision",
]);
const CONTACT_KEYS: ReadonlySet<string> = new Set([
  "phone_e164",
  "email",
  "website",
]);

const DEFAULT_LOCATION: ListingLocation = {
  address_line1: null,
  address_line2: null,
  locality: null,
  region: null,
  postal_code: null,
  country: null,
  lat: null,
  lon: null,
  geo_precision: "none",
};

const DEFAULT_CONTACT: ListingContact = {
  phone_e164: null,
  email: null,
  website: null,
};

// --- handler ---------------------------------------------------------

/**
 * Validate a `listing.upsert` command and resolve it to the event to emit, or
 * to a 422 problem document. `current` is the listing the §7 matching rule
 * selects, or `null` if none exists in the tenant.
 */
export function handleListingUpsert(
  command: OsdsCommand,
  current: Listing | null,
): ListingUpsertResult {
  if (command.command !== COMMAND_NAME) {
    return reject(
      validationProblem(`command "${command.command}" is not ${COMMAND_NAME}`),
    );
  }

  const envelope: string[] = [];
  if (!nonEmptyString(command.idempotency_key))
    envelope.push("idempotency_key is required");
  if (!nonEmptyString(command.tenant_id))
    envelope.push("tenant_id is required");
  if (!nonEmptyString(command.adapter_id))
    envelope.push("adapter_id is required");
  if (!nonEmptyString(command.trace_id)) envelope.push("trace_id is required");
  if (!isObject(command.payload)) envelope.push("payload must be an object");
  if (envelope.length > 0) {
    return reject(validationProblem("malformed command envelope", envelope));
  }

  const p = command.payload as Record<string, unknown>;

  const derived: string[] = [];
  if (has(p, "tier")) {
    derived.push(
      "payload.tier is not accepted - tier is derived from entitlement (§6)",
    );
  }
  if (has(p, "status")) {
    derived.push(
      "payload.status is not accepted - status moves through the claim flow (§9)",
    );
  }
  if (derived.length > 0) {
    return reject(
      validationProblem(
        "listing.upsert does not accept derived fields",
        derived,
      ),
    );
  }

  const errors: string[] = [];
  const input = parseInput(p, current, errors);
  if (errors.length > 0 || input === null) {
    return reject(validationProblem("invalid listing.upsert payload", errors));
  }

  const match: ListingMatch =
    input.id !== undefined
      ? { by: "id", id: input.id }
      : {
          by: "slug",
          tenant_id: command.tenant_id,
          slug: input.slug as string,
        };

  if (current === null) {
    const listing: CreatedListing = {
      id: input.id ?? null,
      tenant_id: command.tenant_id,
      slug: input.slug as string,
      name: input.name as string,
      description: input.description ?? null,
      categories: input.categories ?? [],
      location: overlay(DEFAULT_LOCATION, input.location),
      contact: overlay(DEFAULT_CONTACT, input.contact),
    };
    return {
      outcome: "created",
      match,
      event: { type: "listing.created", data: { listing } },
    };
  }

  const before: ListingContent = contentOf(current);
  const after: ListingContent = {
    slug: input.slug ?? before.slug,
    name: input.name ?? before.name,
    description: has(input, "description")
      ? (input.description ?? null)
      : before.description,
    categories: input.categories ?? before.categories,
    location: overlay(before.location, input.location),
    contact: overlay(before.contact, input.contact),
  };

  const changes = jsonPatch(before, after);
  if (changes.length === 0) {
    return { outcome: "unchanged", match };
  }

  return {
    outcome: "updated",
    match,
    event: { type: "listing.updated", subject: current.id, data: { changes } },
  };
}

// --- parsing --------------------------------------------------------

interface ParsedInput {
  readonly id?: string;
  readonly slug?: string;
  readonly name?: string;
  readonly description?: string | null;
  readonly categories?: readonly string[];
  readonly location?: Partial<ListingLocation>;
  readonly contact?: Partial<ListingContact>;
}

type Mutable<T> = { -readonly [K in keyof T]?: T[K] };

/**
 * Structural validation and normalisation of the payload. Pushes messages onto
 * `errors` and returns `null` when it cannot produce a usable input; otherwise
 * returns the fields the payload actually set, normalised. `slug` and `name`
 * are required only when there is no listing yet - an update may touch a subset.
 */
function parseInput(
  p: Record<string, unknown>,
  current: Listing | null,
  errors: string[],
): ParsedInput | null {
  let id: string | undefined;
  if (has(p, "id")) {
    if (nonEmptyString(p["id"]) && p["id"].startsWith("listing_")) {
      id = p["id"];
    } else {
      errors.push(
        'payload.id, if present, must be a non-empty string prefixed "listing_"',
      );
    }
  }

  let slug: string | undefined;
  if (has(p, "slug")) {
    if (nonEmptyString(p["slug"]) && KEY_SLUG.test(p["slug"])) {
      slug = p["slug"];
    } else {
      errors.push("payload.slug must be a lowercase kebab-case string");
    }
  } else if (current === null) {
    errors.push("payload.slug is required when the listing does not exist");
  }

  let name: string | undefined;
  if (has(p, "name")) {
    if (nonEmptyString(p["name"])) {
      name = p["name"].trim();
    } else {
      errors.push("payload.name must be a non-empty string");
    }
  } else if (current === null) {
    errors.push("payload.name is required when the listing does not exist");
  }

  const out: {
    id?: string;
    slug?: string;
    name?: string;
    description?: string | null;
    categories?: readonly string[];
    location?: Partial<ListingLocation>;
    contact?: Partial<ListingContact>;
  } = {};

  if (id !== undefined) out.id = id;
  if (slug !== undefined) out.slug = slug;
  if (name !== undefined) out.name = name;

  if (has(p, "description")) {
    const d = p["description"];
    if (d === null) {
      out.description = null;
    } else if (typeof d === "string") {
      out.description = d.trim().length > 0 ? d.trim() : null;
    } else {
      errors.push("payload.description must be a string or null");
    }
  }

  if (has(p, "categories")) {
    const c = p["categories"];
    if (
      Array.isArray(c) &&
      c.every((x) => typeof x === "string" && KEY_SLUG.test(x))
    ) {
      out.categories = [...new Set(c as string[])];
    } else {
      errors.push(
        "payload.categories must be an array of lowercase kebab-case strings",
      );
    }
  }

  const location = parseLocation(p, errors);
  if (location !== undefined) out.location = location;

  const contact = parseContact(p, errors);
  if (contact !== undefined) out.contact = contact;

  if (errors.length > 0) return null;
  return out;
}

function parseLocation(
  p: Record<string, unknown>,
  errors: string[],
): Partial<ListingLocation> | undefined {
  if (!has(p, "location")) return undefined;
  const loc = p["location"];
  if (!isObject(loc)) {
    errors.push("payload.location must be an object");
    return undefined;
  }

  const unknown = Object.keys(loc).filter((k) => !LOCATION_KEYS.has(k));
  if (unknown.length > 0) {
    errors.push(`payload.location has unknown field(s): ${unknown.join(", ")}`);
  }

  const out: Mutable<ListingLocation> = {};

  for (const key of [
    "address_line1",
    "address_line2",
    "locality",
    "region",
    "postal_code",
  ] as const) {
    if (!has(loc, key)) continue;
    const v = loc[key];
    if (v === null) out[key] = null;
    else if (typeof v === "string")
      out[key] = v.trim().length > 0 ? v.trim() : null;
    else errors.push(`payload.location.${key} must be a string or null`);
  }

  if (has(loc, "country")) {
    const v = loc["country"];
    if (v === null) out.country = null;
    else if (typeof v === "string" && ALPHA2.test(v.toUpperCase()))
      out.country = v.toUpperCase();
    else
      errors.push(
        "payload.location.country must be an ISO 3166-1 alpha-2 code or null",
      );
  }

  for (const key of ["lat", "lon"] as const) {
    if (!has(loc, key)) continue;
    const v = loc[key];
    const limit = key === "lat" ? 90 : 180;
    if (v === null) out[key] = null;
    else if (
      typeof v === "number" &&
      Number.isFinite(v) &&
      Math.abs(v) <= limit
    )
      out[key] = v;
    else
      errors.push(
        `payload.location.${key} must be a number within +/-${limit} or null`,
      );
  }

  if (has(loc, "geo_precision")) {
    const v = loc["geo_precision"];
    if (typeof v === "string" && GEO_PRECISIONS.has(v))
      out.geo_precision = v as GeoPrecision;
    else
      errors.push(
        'payload.location.geo_precision must be one of "rooftop", "street", "locality", "none"',
      );
  }

  return out;
}

function parseContact(
  p: Record<string, unknown>,
  errors: string[],
): Partial<ListingContact> | undefined {
  if (!has(p, "contact")) return undefined;
  const contact = p["contact"];
  if (!isObject(contact)) {
    errors.push("payload.contact must be an object");
    return undefined;
  }

  const unknown = Object.keys(contact).filter((k) => !CONTACT_KEYS.has(k));
  if (unknown.length > 0) {
    errors.push(`payload.contact has unknown field(s): ${unknown.join(", ")}`);
  }

  const out: Mutable<ListingContact> = {};

  if (has(contact, "phone_e164")) {
    const v = contact["phone_e164"];
    if (v === null) out.phone_e164 = null;
    else if (typeof v === "string" && E164.test(v)) out.phone_e164 = v;
    else
      errors.push("payload.contact.phone_e164 must be an E.164 number or null");
  }

  if (has(contact, "email")) {
    const v = contact["email"];
    if (v === null) out.email = null;
    else if (typeof v === "string" && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v))
      out.email = v.toLowerCase();
    else errors.push("payload.contact.email must be an email address or null");
  }

  if (has(contact, "website")) {
    const v = contact["website"];
    if (v === null) out.website = null;
    else if (typeof v === "string" && /^https?:\/\/\S+$/.test(v))
      out.website = v;
    else errors.push("payload.contact.website must be an http(s) URL or null");
  }

  return out;
}

// --- helpers -------------------------------------------------------

function reject(problem: ProblemDocument): ListingUpsertResult {
  return { outcome: "rejected", problem };
}

function contentOf(listing: Listing): ListingContent {
  return {
    slug: listing.slug,
    name: listing.name,
    description: listing.description,
    categories: listing.categories,
    location: listing.location,
    contact: listing.contact,
  };
}

/** Merge the present keys of `patch` onto `base`; an explicit `null` clears. */
function overlay<T extends object>(base: T, patch: Partial<T> | undefined): T {
  if (patch === undefined) return base;
  const out = { ...base };
  for (const key of Object.keys(patch) as (keyof T)[]) {
    // Object.keys yields only present keys, so the value is defined.
    out[key] = patch[key] as T[keyof T];
  }
  return out;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function nonEmptyString(v: unknown): v is string {
  return typeof v === "string" && v.trim().length > 0;
}

function has(o: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(o, key);
}
