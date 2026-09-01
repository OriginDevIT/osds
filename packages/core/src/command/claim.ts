/**
 * The `claim.submit` and `claim.approve` command handlers - spec §7 (commands),
 * §9 (claim verification), §9.0 (consent), §9.3 (manual verification recording),
 * §9.4 (anti-hijack).
 *
 * Pure, like {@link ./listing-upsert.ts} and {@link ../entitlement.ts}: no
 * database, no clock, no id factory, no I/O. The caller loads the listing and
 * any claim record and passes them in; this module validates the command and
 * returns the events to emit, or a 422 problem document.
 *
 * `claim.submit`
 *   - `consent` is a required field (§9.0, invariant 7). Absent -> rejected,
 *     with no way to opt out to simplify a fixture.
 *   - Normally emits `claim.submitted`, then `claim.verification_started` for
 *     every method except `manual` (which goes straight to admin review).
 *   - `claim.verification_started.expires_at` is computed here (§9.5), never
 *     relayed from the caller: `resolveVerificationTtl` for the method plus the
 *     injected clock. `manual` and `gbp_oauth` have no code, so it is `null`.
 *     A stored TTL outside the §9.5 bounds throws - it is not clamped.
 *   - A submission against a listing that is already `claimed` emits
 *     `claim.disputed` instead and opens moderation (§9.4). Verification never
 *     moves ownership away from a sitting owner, so core never auto-transfers.
 *   - The method must be one the tenant has enabled; the caller passes the list.
 *   - The claim id does not exist yet, so `claim.submitted` / `claim.disputed`
 *     come back as drafts with no `data.claim.id`; the caller mints the id and
 *     calls {@link withClaimId} (cf. listing-upsert's `withSubject`).
 *
 * `claim.approve`
 *   - Emits `claim.approved`, then `listing.owner_assigned`, in that order and
 *     on the same subject so the ordering is guaranteed downstream (§3.1).
 *   - `listing.claimed` is not an event and is not emitted.
 *   - On the `manual` method, `manual_verification.notes` is required and must
 *     be non-empty (§9.3) - an admin who cannot say how they verified has not.
 *   - `claim.notified_existing_contacts` (§9.4) is the adapter runtime's job,
 *     not core's, and is not emitted here.
 *
 * Rate limiting and per-IP throttling live outside core and are out of scope.
 * Idempotency and the 409 replay path land with persistence.
 */
import type { OsdsCommand, ProblemDocument } from "@osds/adapter-kit";
import { validationProblem } from "./problem.js";
import {
  resolveVerificationTtl,
  type VerificationTtlConfig,
} from "./verification-ttl.js";

// --- vocabulary ----------------------------------------------------------

export type ClaimMethod =
  "manual" | "phone_otp" | "domain_email" | "gbp_oauth" | "postcard";

export type ClaimStatus =
  "pending" | "verifying" | "approved" | "rejected" | "abandoned" | "disputed";

/** §9.3 `manual_verification.method_used`. */
export type ManualMethodUsed =
  | "phone"
  | "email"
  | "postcard"
  | "website"
  | "social"
  | "in_person"
  | "document"
  | "other";

/** The listing slice both handlers read. */
export interface ClaimListing {
  readonly id: string;
  readonly tenant_id: string;
  readonly status: "unclaimed" | "claimed" | "suspended";
}

/** An existing claim row, as handed to {@link handleClaimApprove}. */
export interface ClaimRecord {
  readonly id: string;
  readonly tenant_id: string;
  readonly listing_id: string;
  readonly status: ClaimStatus;
  readonly method: ClaimMethod;
  readonly claimant_user_id: string | null;
}

/** One consent channel: whether they agreed, when, from what IP, which wording (§9.0). */
export interface ConsentEntry {
  readonly granted: boolean;
  readonly at: string | null;
  readonly ip: string | null;
  readonly text_version: string;
}

export type ConsentMap = Readonly<Record<string, ConsentEntry>>;

export interface ClaimantData {
  readonly id: string | null;
  readonly name: string;
  readonly email: string;
  readonly phone_e164: string | null;
  readonly role_claimed: string;
}

export interface ManualVerification {
  readonly method_used: ManualMethodUsed;
  readonly verified_by: string;
  readonly verified_at: string;
  readonly notes: string;
  readonly evidence_ref: string | null;
}

// --- events ------------------------------------------------------------

interface NewClaim {
  readonly id: string;
  readonly listing_id: string;
  readonly status: "pending_verification";
  readonly method: ClaimMethod;
}

interface DisputedClaim {
  readonly id: string;
  readonly listing_id: string;
  readonly status: "disputed";
  readonly method: ClaimMethod;
}

interface ApprovedClaim {
  readonly id: string;
  readonly listing_id: string;
  readonly method: ClaimMethod;
}

export interface ClaimSubmittedEvent {
  readonly type: "claim.submitted";
  readonly subject: string;
  readonly data: {
    readonly claim: NewClaim;
    readonly claimant: ClaimantData;
    readonly consent: ConsentMap;
  };
}

export interface ClaimDisputedEvent {
  readonly type: "claim.disputed";
  readonly subject: string;
  readonly data: {
    readonly claim: DisputedClaim;
    readonly claimant: ClaimantData;
    readonly consent: ConsentMap;
  };
}

/** {@link ClaimSubmittedEvent} before the claim id is minted - see {@link withClaimId}. */
export interface ClaimSubmittedDraft {
  readonly type: "claim.submitted";
  readonly subject: string;
  readonly data: {
    readonly claim: Omit<NewClaim, "id">;
    readonly claimant: ClaimantData;
    readonly consent: ConsentMap;
  };
}

/** {@link ClaimDisputedEvent} before the claim id is minted - see {@link withClaimId}. */
export interface ClaimDisputedDraft {
  readonly type: "claim.disputed";
  readonly subject: string;
  readonly data: {
    readonly claim: Omit<DisputedClaim, "id">;
    readonly claimant: ClaimantData;
    readonly consent: ConsentMap;
  };
}

export interface ClaimVerificationStartedEvent {
  readonly type: "claim.verification_started";
  readonly subject: string;
  readonly data: {
    readonly method: ClaimMethod;
    /**
     * Deadline for the method's challenge. Core computes it (§9.5) from the
     * method's TTL and the injected clock; `null` for `manual` / `gbp_oauth`,
     * which have no OSDS-side code.
     */
    readonly expires_at: string | null;
  };
}

export interface ClaimApprovedEvent {
  readonly type: "claim.approved";
  readonly subject: string;
  readonly data: {
    readonly claim: ApprovedClaim;
    readonly decided_by: string;
    readonly manual_verification: ManualVerification | null;
  };
}

export interface ListingOwnerAssignedEvent {
  readonly type: "listing.owner_assigned";
  readonly subject: string;
  readonly data: {
    readonly owner_user_id: string;
    readonly claim_id: string;
  };
}

/**
 * §4.3: emitted when `claim.submit` mints a fresh `users` row, before
 * `claim.submitted` and in the same transaction. Not emitted when an existing
 * row is reused. The pure resolver cannot tell mint from reuse - that is a
 * database fact - so the persistence layer builds this from the claimant data
 * and the minted id.
 */
export interface UserCreatedEvent {
  readonly type: "user.created";
  /** The new `usr_` id. `user.*` is its own subject, not the listing. */
  readonly subject: string;
  readonly data: {
    readonly user: {
      readonly id: string;
      readonly email: string;
      readonly name: string;
      readonly phone_e164: string | null;
    };
    readonly created_by: "claim.submit";
  };
}

// --- results ---------------------------------------------------------

export type ClaimSubmitResult =
  | { readonly outcome: "rejected"; readonly problem: ProblemDocument }
  | {
      readonly outcome: "disputed";
      readonly events: readonly [ClaimDisputedDraft];
    }
  | {
      readonly outcome: "submitted";
      readonly events:
        | readonly [ClaimSubmittedDraft]
        | readonly [ClaimSubmittedDraft, ClaimVerificationStartedEvent];
    };

export type ClaimApproveResult =
  | { readonly outcome: "rejected"; readonly problem: ProblemDocument }
  | {
      readonly outcome: "approved";
      readonly events: readonly [ClaimApprovedEvent, ListingOwnerAssignedEvent];
    };

export type EmittedClaimEvent =
  ClaimSubmittedEvent | ClaimDisputedEvent | ClaimVerificationStartedEvent;

/**
 * Attach the freshly minted claim id to the `claim.submitted` / `claim.disputed`
 * draft in a `claim.submit` result. `claim.verification_started` carries no id
 * and passes through unchanged. The input drafts are not mutated.
 */
export function withClaimId(
  result: Extract<ClaimSubmitResult, { outcome: "submitted" | "disputed" }>,
  claimId: string,
): readonly EmittedClaimEvent[] {
  return result.events.map((event): EmittedClaimEvent => {
    if (event.type === "claim.submitted") {
      return {
        type: "claim.submitted",
        subject: event.subject,
        data: { ...event.data, claim: { ...event.data.claim, id: claimId } },
      };
    }
    if (event.type === "claim.disputed") {
      return {
        type: "claim.disputed",
        subject: event.subject,
        data: { ...event.data, claim: { ...event.data.claim, id: claimId } },
      };
    }
    return event;
  });
}

// --- constants -----------------------------------------------------

const CLAIM_METHODS: ReadonlySet<string> = new Set([
  "manual",
  "phone_otp",
  "domain_email",
  "gbp_oauth",
  "postcard",
]);

const MANUAL_METHODS: ReadonlySet<string> = new Set([
  "phone",
  "email",
  "postcard",
  "website",
  "social",
  "in_person",
  "document",
  "other",
]);

const E164 = /^\+[1-9]\d{1,14}$/;
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// --- claim.submit --------------------------------------------------

/**
 * Validate a `claim.submit` command against the listing and the tenant's
 * enabled verification methods. Returns the events to emit (as drafts - the
 * claim id is minted downstream, see {@link withClaimId}) or a 422 problem.
 *
 * `now` is the injected clock (a resolved instant, never read here) and
 * `ttlConfig` is the tenant's §9.5 `claim_verification.ttl`, threaded like
 * `enabledMethods`; together they fix `claim.verification_started.expires_at`.
 * A TTL outside the §9.5 bounds throws out of this call - it is an operator
 * misconfiguration, not a 422.
 */
export function handleClaimSubmit(
  command: OsdsCommand,
  listing: ClaimListing | null,
  enabledMethods: readonly ClaimMethod[],
  now: Date,
  ttlConfig?: VerificationTtlConfig,
): ClaimSubmitResult {
  if (command.command !== "claim.submit") {
    return reject(
      validationProblem(`command "${command.command}" is not claim.submit`),
    );
  }

  const envelope = envelopeErrors(command);
  if (envelope.length > 0) {
    return reject(validationProblem("malformed command envelope", envelope));
  }
  const p = command.payload as Record<string, unknown>;

  // §9.0 / invariant 7: consent is required, full stop.
  if (!has(p, "consent")) {
    return reject(
      validationProblem("claim.submitted requires a consent record (§9.0)"),
    );
  }

  const errors: string[] = [];
  const parsed = parseSubmit(p, enabledMethods, errors);

  if (listing === null) {
    errors.push("the listing to claim does not exist");
  } else {
    // A suspended listing is not open to a new claim (§4.1 listing status).
    if (listing.status === "suspended") {
      errors.push("the listing is suspended and cannot be claimed");
    }
    if (parsed !== null) {
      if (listing.id !== parsed.listingId) {
        errors.push("payload.listing_id does not match the provided listing");
      }
      if (listing.tenant_id !== command.tenant_id) {
        errors.push("the listing belongs to a different tenant");
      }
    }
  }

  if (errors.length > 0 || parsed === null || listing === null) {
    return reject(validationProblem("invalid claim.submit payload", errors));
  }

  // §9.4: a claim on an already-claimed listing is a dispute, never a transfer.
  if (listing.status === "claimed") {
    return {
      outcome: "disputed",
      events: [
        {
          type: "claim.disputed",
          subject: listing.id,
          data: {
            claim: {
              listing_id: listing.id,
              status: "disputed",
              method: parsed.method,
            },
            claimant: parsed.claimant,
            consent: parsed.consent,
          },
        },
      ],
    };
  }

  const submitted: ClaimSubmittedDraft = {
    type: "claim.submitted",
    subject: listing.id,
    data: {
      claim: {
        listing_id: listing.id,
        status: "pending_verification",
        method: parsed.method,
      },
      claimant: parsed.claimant,
      consent: parsed.consent,
    },
  };

  if (parsed.method === "manual") {
    return { outcome: "submitted", events: [submitted] };
  }

  // §9.5: core computes the deadline. `resolveVerificationTtl` throws on a
  // stored TTL outside the bounds - deliberately not caught here.
  const ttlMs = resolveVerificationTtl(parsed.method, ttlConfig);
  const expiresAt =
    ttlMs === null ? null : new Date(now.getTime() + ttlMs).toISOString();

  return {
    outcome: "submitted",
    events: [
      submitted,
      {
        type: "claim.verification_started",
        subject: listing.id,
        data: { method: parsed.method, expires_at: expiresAt },
      },
    ],
  };
}

interface ParsedSubmit {
  readonly listingId: string;
  readonly method: ClaimMethod;
  readonly claimant: ClaimantData;
  readonly consent: ConsentMap;
}

function parseSubmit(
  p: Record<string, unknown>,
  enabledMethods: readonly ClaimMethod[],
  errors: string[],
): ParsedSubmit | null {
  const before = errors.length;

  const listingId = requireId(p, "listing_id", "listing_", errors);
  const method = parseMethod(p["method"], errors);
  const claimant = parseClaimant(p["claimant"], errors);
  const consent = parseConsent(p["consent"], errors);
  // `verification_expires_at` is no longer read: core computes the deadline
  // from the tenant TTL (§9.5). A stray value in the payload is ignored.

  if (method !== undefined && !enabledMethods.includes(method)) {
    errors.push(
      `verification method "${method}" is not enabled for this tenant`,
    );
  }

  if (
    errors.length > before ||
    listingId === undefined ||
    method === undefined ||
    claimant === null ||
    consent === null
  ) {
    return null;
  }

  return { listingId, method, claimant, consent };
}

// --- claim.approve -----------------------------------------------

/**
 * Validate a `claim.approve` command against the claim and its listing. Returns
 * `[claim.approved, listing.owner_assigned]` in that order, or a 422 problem.
 */
export function handleClaimApprove(
  command: OsdsCommand,
  claim: ClaimRecord | null,
  listing: ClaimListing | null,
): ClaimApproveResult {
  if (command.command !== "claim.approve") {
    return reject(
      validationProblem(`command "${command.command}" is not claim.approve`),
    );
  }

  const envelope = envelopeErrors(command);
  if (envelope.length > 0) {
    return reject(validationProblem("malformed command envelope", envelope));
  }
  const p = command.payload as Record<string, unknown>;

  const errors: string[] = [];
  const parsed = parseApprove(p, errors);

  if (claim === null) errors.push("the claim to approve does not exist");
  if (listing === null) errors.push("the listing does not exist");

  if (claim !== null && parsed !== null && claim.id !== parsed.claimId) {
    errors.push("payload.claim_id does not match the provided claim");
  }
  if (claim !== null && listing !== null && claim.listing_id !== listing.id) {
    errors.push("the claim does not belong to the provided listing");
  }
  if (claim !== null && claim.tenant_id !== command.tenant_id) {
    errors.push("the claim belongs to a different tenant");
  }
  if (
    claim !== null &&
    claim.status !== "pending" &&
    claim.status !== "verifying"
  ) {
    errors.push(`a claim in status "${claim.status}" cannot be approved`);
  }
  if (claim !== null && claim.claimant_user_id === null) {
    errors.push("the claim has no claimant user to assign as owner");
  }

  if (claim !== null && parsed !== null) {
    if (claim.method === "manual" && parsed.manualVerification === null) {
      errors.push(
        "manual_verification with non-empty notes is required to approve a manual claim (§9.3)",
      );
    }
    if (claim.method !== "manual" && parsed.manualVerification !== null) {
      errors.push(
        "manual_verification is only valid when the claim was verified manually",
      );
    }
  }

  if (
    errors.length > 0 ||
    parsed === null ||
    claim === null ||
    listing === null ||
    claim.claimant_user_id === null
  ) {
    return reject(validationProblem("invalid claim.approve payload", errors));
  }

  const ownerUserId = claim.claimant_user_id;

  return {
    outcome: "approved",
    events: [
      {
        type: "claim.approved",
        subject: listing.id,
        data: {
          claim: { id: claim.id, listing_id: listing.id, method: claim.method },
          decided_by: parsed.decidedBy,
          manual_verification: parsed.manualVerification,
        },
      },
      {
        type: "listing.owner_assigned",
        subject: listing.id,
        data: { owner_user_id: ownerUserId, claim_id: claim.id },
      },
    ],
  };
}

interface ParsedApprove {
  readonly claimId: string;
  readonly decidedBy: string;
  readonly manualVerification: ManualVerification | null;
}

function parseApprove(
  p: Record<string, unknown>,
  errors: string[],
): ParsedApprove | null {
  const before = errors.length;

  const claimId = requireId(p, "claim_id", "claim_", errors);
  const decidedBy = requireId(p, "decided_by", "usr_", errors);

  let manualVerification: ManualVerification | null = null;
  if (has(p, "manual_verification") && p["manual_verification"] !== null) {
    manualVerification = parseManualVerification(
      p["manual_verification"],
      errors,
    );
  }

  if (
    errors.length > before ||
    claimId === undefined ||
    decidedBy === undefined
  ) {
    return null;
  }

  return { claimId, decidedBy, manualVerification };
}

function parseManualVerification(
  raw: unknown,
  errors: string[],
): ManualVerification | null {
  if (!isObject(raw)) {
    errors.push("payload.manual_verification must be an object");
    return null;
  }
  const before = errors.length;

  const methodUsed = raw["method_used"];
  if (typeof methodUsed !== "string" || !MANUAL_METHODS.has(methodUsed)) {
    errors.push(
      `payload.manual_verification.method_used must be one of ${[...MANUAL_METHODS].join(", ")}`,
    );
  }

  const verifiedBy = raw["verified_by"];
  if (!nonEmptyString(verifiedBy) || !verifiedBy.startsWith("usr_")) {
    errors.push('payload.manual_verification.verified_by must be a "usr_" id');
  }

  const verifiedAt = raw["verified_at"];
  if (!isIsoInstant(verifiedAt)) {
    errors.push(
      "payload.manual_verification.verified_at must be an RFC 3339 timestamp",
    );
  }

  // §9.3: notes is required, not optional.
  const notes = raw["notes"];
  if (!nonEmptyString(notes)) {
    errors.push(
      "payload.manual_verification.notes is required and must not be empty (§9.3)",
    );
  }

  let evidenceRef: string | null = null;
  const rawEvidence = raw["evidence_ref"];
  if (has(raw, "evidence_ref") && rawEvidence !== null) {
    if (nonEmptyString(rawEvidence)) {
      evidenceRef = rawEvidence.trim();
    } else {
      errors.push(
        "payload.manual_verification.evidence_ref must be a non-empty string or null",
      );
    }
  }

  if (errors.length > before) return null;

  return {
    method_used: methodUsed as ManualMethodUsed,
    verified_by: (verifiedBy as string).trim(),
    verified_at: verifiedAt as string,
    notes: (notes as string).trim(),
    evidence_ref: evidenceRef,
  };
}

// --- shared parsing --------------------------------------------

function parseClaimant(raw: unknown, errors: string[]): ClaimantData | null {
  if (!isObject(raw)) {
    errors.push("payload.claimant must be an object");
    return null;
  }
  const before = errors.length;

  let id: string | null = null;
  const rawId = raw["id"];
  if (has(raw, "id") && rawId !== null) {
    if (nonEmptyString(rawId) && rawId.startsWith("usr_")) {
      id = rawId;
    } else {
      errors.push(
        'payload.claimant.id, if present, must be a "usr_" id or null',
      );
    }
  }

  const name = raw["name"];
  if (!nonEmptyString(name)) errors.push("payload.claimant.name is required");

  const email = raw["email"];
  if (!nonEmptyString(email) || !EMAIL.test(email)) {
    errors.push("payload.claimant.email must be a valid email address");
  }

  let phone: string | null = null;
  const rawPhone = raw["phone_e164"];
  if (has(raw, "phone_e164") && rawPhone !== null) {
    if (typeof rawPhone === "string" && E164.test(rawPhone)) {
      phone = rawPhone;
    } else {
      errors.push(
        "payload.claimant.phone_e164 must be an E.164 number or null",
      );
    }
  }

  const role = raw["role_claimed"];
  if (!nonEmptyString(role)) {
    errors.push("payload.claimant.role_claimed is required");
  }

  if (errors.length > before) return null;

  return {
    id,
    name: (name as string).trim(),
    email: (email as string).toLowerCase(),
    phone_e164: phone,
    role_claimed: (role as string).trim(),
  };
}

function parseConsent(raw: unknown, errors: string[]): ConsentMap | null {
  if (!isObject(raw)) {
    errors.push("payload.consent must be an object");
    return null;
  }
  const channels = Object.keys(raw);
  if (channels.length === 0) {
    errors.push("payload.consent must record at least one channel");
    return null;
  }
  const before = errors.length;

  const out: Record<string, ConsentEntry> = {};
  for (const channel of channels) {
    const entry = raw[channel];
    if (!isObject(entry)) {
      errors.push(`payload.consent.${channel} must be an object`);
      continue;
    }

    const granted = entry["granted"];
    if (typeof granted !== "boolean") {
      errors.push(`payload.consent.${channel}.granted must be a boolean`);
      continue;
    }

    const textVersion = entry["text_version"];
    if (!nonEmptyString(textVersion)) {
      errors.push(`payload.consent.${channel}.text_version is required`);
    }

    const at = readConsentField(
      entry,
      "at",
      channel,
      granted,
      errors,
      isIsoInstant,
    );
    const ip = readConsentField(
      entry,
      "ip",
      channel,
      granted,
      errors,
      nonEmptyString,
    );

    out[channel] = {
      granted,
      at,
      ip,
      text_version: nonEmptyString(textVersion) ? textVersion.trim() : "",
    };
  }

  if (errors.length > before) return null;
  return out;
}

/**
 * A consent field that must be present, may be `null` only when consent was not
 * granted, and otherwise must satisfy `valid` (§9.0). Returns the normalised
 * value or `null`.
 */
function readConsentField(
  entry: Record<string, unknown>,
  key: "at" | "ip",
  channel: string,
  granted: boolean,
  errors: string[],
  valid: (v: unknown) => v is string,
): string | null {
  if (!has(entry, key)) {
    errors.push(`payload.consent.${channel}.${key} is required`);
    return null;
  }
  const v = entry[key];
  if (v === null) {
    if (granted) {
      errors.push(
        `payload.consent.${channel}.${key} is required when consent is granted`,
      );
    }
    return null;
  }
  if (valid(v)) return key === "ip" ? v.trim() : v;
  errors.push(
    `payload.consent.${channel}.${key} must be ${key === "at" ? "an RFC 3339 timestamp" : "a non-empty string"} or null`,
  );
  return null;
}

// --- helpers ---------------------------------------------------

function reject(problem: ProblemDocument): {
  readonly outcome: "rejected";
  readonly problem: ProblemDocument;
} {
  return { outcome: "rejected", problem };
}

function envelopeErrors(command: OsdsCommand): string[] {
  const errors: string[] = [];
  if (!nonEmptyString(command.idempotency_key)) {
    errors.push("idempotency_key is required");
  }
  if (!nonEmptyString(command.tenant_id)) errors.push("tenant_id is required");
  if (!nonEmptyString(command.adapter_id))
    errors.push("adapter_id is required");
  if (!nonEmptyString(command.trace_id)) errors.push("trace_id is required");
  if (!isObject(command.payload)) errors.push("payload must be an object");
  return errors;
}

function requireId(
  p: Record<string, unknown>,
  key: string,
  prefix: string,
  errors: string[],
): string | undefined {
  const v = p[key];
  if (nonEmptyString(v) && v.startsWith(prefix)) return v;
  errors.push(`payload.${key} must be a non-empty string prefixed "${prefix}"`);
  return undefined;
}

function parseMethod(v: unknown, errors: string[]): ClaimMethod | undefined {
  if (typeof v === "string" && CLAIM_METHODS.has(v)) return v as ClaimMethod;
  errors.push(
    'payload.method must be one of "manual", "phone_otp", "domain_email", "gbp_oauth", "postcard"',
  );
  return undefined;
}

function isIsoInstant(v: unknown): v is string {
  return typeof v === "string" && !Number.isNaN(Date.parse(v));
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
