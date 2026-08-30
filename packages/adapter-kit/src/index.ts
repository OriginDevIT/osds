/**
 * @osds/adapter-kit - the contract between OSDS core and adapter code.
 *
 * Types only, no dependencies. Core emits events and accepts commands;
 * adapters translate. Tracks docs/spec/events-and-adapters.md v0.4,
 * sections 2 (envelope), 3.3 (event catalogue), 7 (commands) and 8 (adapter
 * interface).
 */

// ---------------------------------------------------------------------------
// 2. Event envelope  (core -> adapter)
// ---------------------------------------------------------------------------

/** Who or what caused an event. Spec section 2, `actor.type`. */
export type ActorType =
  | "visitor"
  | "owner"
  | "staff"
  | "admin"
  | "system"
  | "agent"
  | "adapter";

export interface EventActor {
  readonly type: ActorType;
  readonly id: string | null;
  readonly ip: string | null;
  readonly user_agent: string | null;
}

export interface EventTenant {
  readonly id: string;
  readonly slug: string;
  readonly domain: string | null;
}

/**
 * Fields common to every event. Facts, past tense, immutable - never retracted,
 * only followed by a corrective event. Delivery is at-least-once; adapters
 * dedupe on `id`.
 */
interface BaseEvent {
  /** ULID. Doubles as the idempotency key. */
  readonly id: string;
  /** Schema major version for this event type, not for OSDS as a whole. */
  readonly version: number;
  /** RFC 3339, UTC, millisecond precision. */
  readonly occurred_at: string;
  /** Primary entity. Ordering is guaranteed per `subject`, never globally. */
  readonly subject: string;
  readonly actor: EventActor;
  /** Adapter ID that caused this event via a command, or `null` if core-originated. Loop guard. */
  readonly origin: string | null;
  /** Propagates across command -> event -> command chains. */
  readonly trace_id: string;
  readonly data: Readonly<Record<string, unknown>>;
}

/**
 * Every event except `tenant.*`. The `tenant` block is always present, so
 * adapters never null-check it.
 */
export interface OsdsEvent extends BaseEvent {
  /** `<domain>.<past-tense-verb>`. Permanent - renaming means a new type. */
  readonly type: Exclude<EventType, TenantEventType>;
  readonly tenant: EventTenant;
}

/**
 * `tenant.*` events. The tenant is the subject, so the envelope carries no
 * `tenant` block (§3.3).
 */
export interface TenantEvent extends BaseEvent {
  readonly type: TenantEventType;
}

/** Either envelope shape - what the adapter runtime delivers to `handle`. */
export type OsdsAnyEvent = OsdsEvent | TenantEvent;

// ---------------------------------------------------------------------------
// 3.3 Complete event catalogue
//
// The canonical list is the §3.3 table; this union is generated from it, one
// exported union per namespace (§3.2). §3.4 deferred events (`media.*`,
// `search.*`) are intentionally excluded - adding them later is additive.
// Type names are permanent.
// ---------------------------------------------------------------------------

/** `listing.*` - the listing record and its published state. */
export type ListingEventType =
  | "listing.created"
  | "listing.updated"
  | "listing.published"
  | "listing.unpublished"
  | "listing.merged"
  | "listing.deleted"
  | "listing.owner_assigned"
  | "listing.tier_changed"
  | "listing.expiring_soon"
  | "listing.expired";

/** `claim.*` - acquiring a verified human owner. */
export type ClaimEventType =
  | "claim.submitted"
  | "claim.verification_started"
  | "claim.verification_failed"
  | "claim.approved"
  | "claim.rejected"
  | "claim.abandoned"
  | "claim.notified_existing_contacts"
  | "claim.disputed";

/** `billing.*` - money, as reported by a payment adapter. Never sets tier directly. */
export type BillingEventType =
  | "billing.checkout_started"
  | "billing.subscription_started"
  | "billing.subscription_changed"
  | "billing.payment_succeeded"
  | "billing.payment_failed"
  | "billing.subscription_canceled"
  | "billing.refund_issued";

/** `entitlement.*` - tier and period state, as decided by core. Detail in §6.10. */
export type EntitlementEventType =
  | "entitlement.started"
  | "entitlement.trial_converted"
  | "entitlement.dunning_started"
  | "entitlement.recovered"
  | "entitlement.downgraded"
  | "entitlement.restored"
  | "entitlement.renewal_due"
  | "entitlement.expired"
  | "entitlement.canceled"
  | "entitlement.overridden";

/** `slot.*` - capacity-limited premium placement. Detail in §6.6. */
export type SlotEventType =
  | "slot.held"
  | "slot.hold_released"
  | "slot.occupied"
  | "slot.released"
  | "slot.waitlist_joined"
  | "slot.waitlist_notified"
  | "slot.waitlist_cleared"
  | "slot.capacity_changed";

/** `lead.*` - consumer contact delivered to a business. `lead.captured` requires `consent`. */
export type LeadEventType =
  | "lead.captured"
  | "lead.delivered"
  | "lead.delivery_failed"
  | "lead.marked_spam";

/** `call.*` - tracked phone contact delivered to a business. */
export type CallEventType = "call.tracked";

/** `review.*` - native reviews. Detail in §5.3. */
export type ReviewEventType =
  | "review.submitted"
  | "review.published"
  | "review.flagged"
  | "review.removed"
  | "review.responded";

/** `moderation.*` - human or agent decisions on queued items. */
export type ModerationEventType =
  | "moderation.queued"
  | "moderation.decided";

/** `compliance.*` - removal, export, consent. */
export type ComplianceEventType =
  | "compliance.removal_requested"
  | "compliance.removal_completed"
  | "compliance.data_exported"
  | "compliance.consent_changed";

/** `agent.*` - AI agent actions and escalation. Detail in §8.5. */
export type AgentEventType =
  | "agent.action_taken"
  | "agent.escalation_requested"
  | "agent.escalation_resolved"
  | "agent.blocked";

/** `tenant.*` - directory lifecycle. The only namespace not tenant-scoped. */
export type TenantEventType =
  | "tenant.created"
  | "tenant.domain_verified"
  | "tenant.settings_changed"
  | "tenant.suspended";

/** `import.*` - CSV batches and their rollback. */
export type ImportEventType =
  | "import.started"
  | "import.completed"
  | "import.rolled_back";

/** `postal.*` - physical mail dispatch. */
export type PostalEventType =
  | "postal.dispatched"
  | "postal.failed";

export type EventType =
  | ListingEventType
  | ClaimEventType
  | BillingEventType
  | EntitlementEventType
  | SlotEventType
  | LeadEventType
  | CallEventType
  | ReviewEventType
  | ModerationEventType
  | ComplianceEventType
  | AgentEventType
  | TenantEventType
  | ImportEventType
  | PostalEventType;

// ---------------------------------------------------------------------------
// 7. Command envelope  (adapter -> core)
// ---------------------------------------------------------------------------

/**
 * Commands adapters send into core. Core validates, may reject, and emits the
 * resulting event if it succeeds. `listing.setTier` is deliberately absent -
 * tier is derived from entitlement, never set directly.
 */
export type CommandName =
  | "listing.upsert"
  | "listing.setVisibility"
  | "listing.merge"
  | "listing.attachMedia"
  | "claim.submit"
  | "claim.approve"
  | "claim.reject"
  | "lead.create"
  | "lead.markSpam"
  | "review.submit"
  | "review.respond"
  | "review.flag"
  | "entitlement.grant"
  | "entitlement.revoke"
  | "entitlement.reportPayment"
  | "slot.hold"
  | "slot.release"
  | "consent.record"
  | "moderation.enqueue"
  | "moderation.decide";

export interface OsdsCommand {
  readonly command: CommandName;
  /** Derived from the external system's identifiers so a redelivery collapses to one effect. */
  readonly idempotency_key: string;
  readonly tenant_id: string;
  readonly adapter_id: string;
  /** Propagated onto the resulting event's `trace_id`. */
  readonly trace_id: string;
  readonly payload: Readonly<Record<string, unknown>>;
}

/** RFC 7807-style validation problem returned on a rejected command (422). */
export interface ProblemDocument {
  readonly title: string;
  readonly type?: string;
  readonly detail?: string;
  readonly [key: string]: unknown;
}

/**
 * Outcome of a command. `202` -> accepted, `409` -> idempotency replay
 * (carries the original event ID, treated as success), `422` -> rejected.
 */
export type CommandResult =
  | { readonly status: "accepted"; readonly event_id: string }
  | { readonly status: "duplicate"; readonly event_id: string }
  | { readonly status: "rejected"; readonly problem: ProblemDocument };

export interface CommandClient {
  send(command: OsdsCommand): Promise<CommandResult>;
}

// ---------------------------------------------------------------------------
// 8. Adapter interface
// ---------------------------------------------------------------------------

export type Capability =
  | "email.send"
  | "sms.send"
  | "voice.call"
  | "postal.send" // physical mail, for postcard verification
  | "crm.sync_contact"
  | "payments.checkout"
  | "payments.subscription"
  | "media.store"
  | "agent.converse"
  | "analytics.track"
  | "reviews.fetch" // OPTIONAL - external review display, §5.1
  | "search.index"; // OPTIONAL upgrade - core search always works without it

export type Scope =
  | "pii:contact"
  | "pii:message"
  | "command:listing"
  | "command:claim"
  | "command:entitlement"
  | "command:moderation";

/**
 * Local placeholder for a JSON Schema draft-07 document. Kept structural to keep
 * the kit dependency-free; adapter authors may narrow it with `@types/json-schema`.
 */
export type JSONSchema7 = Readonly<Record<string, unknown>>;

export interface AdapterManifest {
  id: string; // "stripe" - stable, lowercase, no vendor version
  name: string;
  version: string;
  osds_api: string; // semver range, e.g. "^1.0.0"
  bundled?: boolean; // ships in-repo; see §8.6
  default_enabled?: boolean; // enabled on a fresh install
  capabilities: Capability[];
  scopes: Scope[];
  subscribes: string[]; // ["claim.*", "listing.tier_changed"]
  config_schema: JSONSchema7;
  secrets: string[];
  inbound_routes?: string[];
  egress_allowlist: string[];
  homepage?: string;
  license: string;
}

/** Deployment-level first, per-tenant override; throws `ConfigurationError` when unset. §8.1. */
export interface SecretResolver {
  get(name: string): Promise<string>;
}

/** Auto-redacts declared PII fields. Never log `contact`, `consent` or message bodies. */
export interface Logger {
  debug(message: string, fields?: Record<string, unknown>): void;
  info(message: string, fields?: Record<string, unknown>): void;
  warn(message: string, fields?: Record<string, unknown>): void;
  error(message: string, fields?: Record<string, unknown>): void;
}

/** Adapter-scoped and tenant-scoped. Where adapters map external IDs to OSDS IDs. */
export interface KeyValueStore {
  get(key: string): Promise<string | null>;
  set(key: string, value: string): Promise<void>;
  delete(key: string): Promise<void>;
}

export interface FetchRequestInit {
  method?: string;
  headers?: Record<string, string>;
  body?: string | null;
  signal?: unknown;
}

export interface FetchResponse {
  readonly ok: boolean;
  readonly status: number;
  readonly statusText: string;
  readonly headers: { get(name: string): string | null };
  text(): Promise<string>;
  json(): Promise<unknown>;
  arrayBuffer(): Promise<ArrayBuffer>;
}

/**
 * Instrumented, egress-allowlisted fetch. Hosts outside `egress_allowlist` are
 * refused. Shaped structurally so the kit pulls in no DOM or Node typings.
 */
export type FetchLike = (
  input: string,
  init?: FetchRequestInit,
) => Promise<FetchResponse>;

export interface AdapterContext {
  tenant: { id: string; slug: string; domain: string | null };
  config: Record<string, unknown>;
  secrets: SecretResolver;
  command: CommandClient;
  logger: Logger; // auto-redacts declared PII fields
  kv: KeyValueStore; // adapter-scoped and tenant-scoped
  http: FetchLike; // instrumented, egress-allowlisted
  clock: () => Date;
}

export type HandleResult =
  | { status: "ok"; note?: string }
  | { status: "skipped"; reason: string }
  | { status: "retry"; after_ms: number; reason: string }
  | { status: "failed"; reason: string; permanent: true };

/** Inbound HTTP hit on one of the adapter's `inbound_routes`. §8.4. */
export interface InboundRequest {
  method: string;
  path: string;
  headers: Record<string, string>;
  query: Record<string, string>;
  /** Raw request body, verbatim - `X-OSDS-Signature` is an HMAC over these bytes. */
  body: string;
}

export interface InboundResult {
  status: number;
  headers?: Record<string, string>;
  body?: string;
}

export interface Adapter {
  manifest: AdapterManifest;
  init?(ctx: AdapterContext): Promise<void>;
  handle(event: OsdsAnyEvent, ctx: AdapterContext): Promise<HandleResult>;
  actions?: Record<string, (input: unknown, ctx: AdapterContext) => Promise<unknown>>;
  inbound?(req: InboundRequest, ctx: AdapterContext): Promise<InboundResult>;
  health?(ctx: AdapterContext): Promise<{ ok: boolean; detail?: string }>;
}
