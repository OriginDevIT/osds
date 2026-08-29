# OSDS - Event Schema, Adapter Interface & Entitlements

**Open Source Directory Site**
**Status:** Draft v0.3 · **License:** Apache-2.0 · **Steward:** Origin Development & IT, Inc.
**Audience:** core maintainers, adapter authors

This document defines the contract between the OSDS core and everything outside it. The core is a multi-tenant directory engine. It knows nothing about email providers, CRMs, payment gateways, or messaging platforms. It emits facts and accepts commands. Adapters translate.

If you are writing an adapter, sections 3, 6 and 7 are the ones you need.
If you are implementing the paid tiers, section 5 is the whole job.

### Changes from v0.2

- `location.address_line2`, `contact.social[]`, `listing.external_profiles` added (§3.1).
- Review system specified: native and external, per-tenant toggles, per-listing override (§4).
- **Entitlement state machine** - the substance of this revision (§5).
- **Premium slot system** - capacity, terms, waitlist, unsold-slot behaviour (§5.6).
- Claim verification methods, manual fallback, and anti-hijack measures (§8).
- Bundled adapters concept: `smtp` and `webhook` ship enabled; payment adapters ship in-repo, opt-in (§7.6).
- Sitemap index required from day one (§11).

---

## 1. Design rules

Load-bearing. Breaking these is how the project ends up with a proprietary dependency in the core.

1. **Core never imports adapter code.** No conditional logic anywhere in core that names a vendor.
2. **Events are facts, past tense, immutable.** An emitted event is never retracted, only followed by a corrective event.
3. **Commands are requests, imperative, fallible.** Adapters send commands *into* core. Core validates, may reject, and emits the resulting event if it succeeds.
4. **Every event is tenant-scoped.** No global events except `tenant.*`.
5. **Delivery is at-least-once.** Adapters must be idempotent on `event.id`.
6. **Single-tenant is the default experience, multi-tenant is the schema.** Every table carries `tenant_id` from the first migration. Single-directory mode is a UI toggle, never a different data model.
7. **PII is opt-in per adapter.** Core redacts by default.
8. **No data-source connectors.** OSDS provides the directory system, not the means to populate it (§3.1.1).
9. **Core owns entitlement state; adapters own money.** Core decides what tier a listing is on and until when. A payment adapter reports what happened financially and never writes tier directly.

---

## 2. Envelope

```json
{
  "id": "01JBQ7X2M4K8ZP3RVN6T9WGYHD",
  "type": "listing.claimed",
  "version": 1,
  "occurred_at": "2026-08-28T14:22:10.442Z",
  "subject": "listing_01JBQ6YW8TFN2H5CKXQ4V3ZDAE",

  "tenant": { "id": "tnt_01JBQ2K9", "slug": "chicago-plumbers", "domain": "chicagoplumbers.example" },
  "actor":  { "type": "owner", "id": "usr_01JBQ5T2", "ip": "203.0.113.44", "user_agent": "Mozilla/5.0 ..." },
  "origin": null,
  "trace_id": "01JBQ7X2M4K8ZP3RVN6T9WGYHD",

  "data": { }
}
```

| Field | Purpose |
|---|---|
| `id` | ULID. Doubles as the idempotency key. Adapters must dedupe on it. |
| `type` | `<domain>.<past-tense-verb>`. Permanent - renaming means a new event plus deprecation of the old. |
| `version` | Schema major version **for this event type**, not for OSDS as a whole. |
| `occurred_at` | RFC 3339, UTC, millisecond precision. |
| `subject` | Primary entity. Ordering guaranteed per `subject`, never globally. |
| `actor.type` | `visitor` \| `owner` \| `staff` \| `admin` \| `system` \| `agent` \| `adapter` |
| `origin` | Adapter ID that caused this event via a command, or `null` if core-originated. **Loop guard.** |
| `trace_id` | Propagates across command → event → command chains. |

### 2.1 Loop prevention

- Core stamps `origin` with the adapter ID whenever an event results from that adapter's command.
- **An adapter MUST ignore any event whose `origin` equals its own ID.** The runtime filters these; adapters assert anyway.
- For multi-adapter cycles, an adapter that sees its own `trace_id` return drops the event.

---

## 3. Core entities

### 3.1 Listing

```jsonc
{
  "id": "listing_01JBQ6YW8TFN2H5CKXQ4V3ZDAE",
  "slug": "hoffman-plumbing-lakeview",
  "name": "Hoffman Plumbing",
  "status": "unclaimed",              // unclaimed | claimed | suspended
  "visibility": "draft",              // draft | published | hidden
  "tier": "free",                     // resolved from entitlement - see §5
  "categories": ["plumbers", "emergency-plumbers"],

  "location": {
    "address_line1": "1422 W Belmont Ave",
    "address_line2": "Suite 200",     // nullable
    "locality": "Chicago",
    "region": "IL",
    "postal_code": "60657",
    "country": "US",
    "lat": 41.9395,
    "lon": -87.6640,
    "geo_precision": "rooftop"        // rooftop | street | locality | none
  },

  "contact": {                        // REDACTED unless adapter holds pii:contact
    "phone_e164": "+17735550142",
    "email": "office@hoffmanplumbing.example",
    "website": "https://hoffmanplumbing.example",
    "social": [
      { "platform": "facebook",  "url": "https://facebook.com/hoffmanplumbing", "label": null },
      { "platform": "instagram", "url": "https://instagram.com/hoffmanplumbing", "label": null }
    ]
  },

  "external_profiles": {
    "google":   { "place_id": "ChIJN1t_tDeuEmsRUsoyG83frY4", "map_url": null, "review_url": null },
    "facebook": { "page_url": "https://facebook.com/hoffmanplumbing", "review_url": null },
    "yelp":     { "business_url": null },
    "bbb":      { "profile_url": null }
  },

  "attributes": { "hours": {}, "payment_methods": [], "service_area_radius_km": 25 },
  "media": { "logo": null, "cover": null, "gallery": [] },
  "reviews_disabled": false           // per-listing override, moderation use
}
```

**`contact.social` is an ordered array**, not fixed fields, so adding a platform is configuration rather than migration. `platform` is an open string with a suggested vocabulary; the admin UI offers known platforms and accepts others.

**Google links are derived from `place_id`.** Both the map deep link and the write-a-review link are constructible from a Place ID, and pasted URLs rot when link formats change. `map_url` and `review_url` exist as explicit overrides for listings that have a link but no Place ID. Storing a pointer is not storing data - this does not touch §3.1.1.

#### 3.1.1 On data sources - a deliberate non-feature

**OSDS ships no connectors to any external listing dataset.** No mapping-provider imports, no scrapers, no "populate my directory" button, and no plugin hook a third party could use to add one.

The operator is responsible for the listings they publish. OSDS provides manual entry, CSV upload, owner submission and the write API. Where the operator's data comes from is the operator's decision and the operator's liability.

This is a design position, not a resourcing gap. Contributors proposing a data-source connector should be pointed here; the answer is no. Mirror this section into `CONTRIBUTING.md`.

`provenance` therefore exists for four operational reasons, none about upstream licensing:

- **Dedupe** - CSV import, owner submission and manual entry for the same business must be distinguishable to merge sensibly.
- **Undo** - "roll back import batch 47" needs `import_batch_id`.
- **Removal that sticks** - `listing.deleted` carries a `suppression_key` (normalised name + address + phone hash) that subsequent imports check against, so a removed business does not reappear on the next CSV upload.
- **Trust display** - "owner-verified" versus "added by editor" is information the public page should surface.

```jsonc
"provenance": {
  "source": "csv_import",            // manual | csv_import | owner_submission | api
  "import_batch_id": "imp_01JBQ...",
  "submitted_by": "usr_01JBQ...",
  "created_at": "2026-08-01T09:00:00Z",
  "notes": null
}
```

### 3.2 Tiers are tenant-configured

Core does not hardcode `free`/`featured`/`premium`. A tenant defines an ordered tier list:

```jsonc
"tiers": [
  { "key": "free",     "rank": 0, "purchasable": false, "uses_slot": false },
  { "key": "verified", "rank": 1, "purchasable": true,  "uses_slot": false },
  { "key": "featured", "rank": 2, "purchasable": true,  "uses_slot": true  }
]
```

`rank 0` is the fallback tier. A tenant may define **no rank-0 tier**, meaning there is no free listing - this changes downgrade behaviour (§5.4).

---

## 4. Reviews

Reviews are configured per tenant. The same installation can run `localplumbers.example` with native reviews on and `localhvac.example` with native reviews off and Google as the review destination.

```jsonc
// tenant settings
"reviews": {
  "native": {
    "enabled": true,
    "moderation": "pre",                // pre | post | none
    "allow_owner_response": true,
    "require_verified_email": true
  },
  "external": {
    "leave_review_targets": ["google", "facebook"],   // ordered; renders as buttons
    "display_ratings_from": []                        // requires an installed adapter
  }
}
```

### 4.1 The line between linking and displaying

**Linking out is always safe and always core.** A "Leave a review on Google" button pointing at the listing's review URL is a hyperlink. No adapter, no API key, no terms to accept. This is what most operators actually want, and it works on a bare install.

**Displaying external review content is different.** Fetching a provider's ratings and review text and rendering them on your pages is governed by that provider's API terms, which typically restrict caching and display. Same reasoning as §3.1.1: core does not ship it. An operator who wants it installs an optional adapter, brings their own API credentials, and accepts those terms themselves.

`display_ratings_from` renders nothing when no corresponding adapter is installed. A default install is legally boring.

### 4.2 Per-listing override

`listing.reviews_disabled` shuts off reviews on a single listing independently of the tenant setting. You will need this the first time a listing attracts a brigade, and site-wide is the wrong lever.

### 4.3 Events

| Type | Notable data |
|---|---|
| `review.submitted` | `review`, `verification` |
| `review.published` | `review`, `rating_snapshot` |
| `review.flagged` | `reason`, `flagged_by` |
| `review.removed` | `reason`, `decided_by`, `legal_hold` |
| `review.responded` | `response`, `responder_id` |

Reviews carry defamation exposure. `review.removed` with `legal_hold: true` retains the record and hides the display. Never hard-delete a review under legal hold.

---

## 5. Entitlements

**The rule:** core owns entitlement, adapters own money. A payment adapter reports `billing.payment_succeeded`; core decides what that means for the listing and emits `listing.tier_changed`. Adapters that care about presentation listen to `listing.tier_changed`, never to billing events.

This separation is why swapping Stripe for PayPal for GHL payments never touches the entitlement logic.

### 5.1 Entitlement record

```jsonc
{
  "id": "ent_01JBQ...",
  "listing_id": "listing_01JBQ...",
  "tier": "featured",
  "status": "active",
  "billing_mode": "recurring",        // recurring | term | comp | none
  "term_days": null,                  // 30 | 60 | 90 | 365 for billing_mode=term
  "started_at": "2026-08-01T00:00:00Z",
  "current_period_end": "2026-09-01T00:00:00Z",
  "trial_ends_at": null,
  "dunning_started_at": null,
  "grace_ends_at": null,
  "slot_id": "slot_01JBQ...",         // null if tier.uses_slot is false
  "cancel_at_period_end": false,
  "comp": null,                       // { granted_by, reason, expires_at | null }
  "payment_ref": { "adapter": "stripe", "external_id": "sub_1QxYz" }
}
```

### 5.2 States

| Status | Meaning |
|---|---|
| `none` | No entitlement. Listing sits on the rank-0 tier. |
| `trialing` | Trial running, card on file, converts automatically at `trial_ends_at`. |
| `active` | Paid and current. |
| `past_due` | Payment failed. Dunning window running. **Premium features remain fully visible.** |
| `grace` | Dunning exhausted. Perks withdrawn, restore path open for 30 days. |
| `expired` | Grace ended, or a term ended without renewal. |
| `canceled` | Owner cancelled. Runs to `current_period_end`, then expires. |
| `comped` | Admin-granted. Optional expiry. |

### 5.3 Transitions

| From | Trigger | To | Emits |
|---|---|---|---|
| `none` | `billing.checkout_started` completes, trial configured | `trialing` | `entitlement.started`, `listing.tier_changed` |
| `none` | Checkout completes, no trial | `active` | `entitlement.started`, `listing.tier_changed` |
| `trialing` | `trial_ends_at` reached, payment succeeds | `active` | `entitlement.trial_converted` |
| `trialing` | `trial_ends_at` reached, payment fails | `past_due` | `entitlement.dunning_started` |
| `trialing` | Owner cancels | `canceled` | `entitlement.canceled` |
| `active` | `billing.payment_failed` | `past_due` | `entitlement.dunning_started` |
| `active` | Owner cancels | `canceled` | `entitlement.canceled` |
| `active` | `billing.refund_issued` | `expired` | `listing.tier_changed` (immediate) |
| `active` | Term ends, `billing_mode=term`, not renewed | `expired` | `entitlement.expired`, `listing.tier_changed` |
| `past_due` | Payment succeeds | `active` | `entitlement.recovered` |
| `past_due` | 14 days elapsed | `grace` | `entitlement.downgraded`, `listing.tier_changed` |
| `grace` | Owner pays | `active` | `entitlement.restored`, `listing.tier_changed` |
| `grace` | 30 days elapsed | `expired` | `entitlement.expired` |
| `canceled` | `current_period_end` reached | `expired` | `entitlement.expired`, `listing.tier_changed` |
| `comped` | `comp.expires_at` reached | `expired` | `entitlement.expired` |
| any | Admin override | any | `entitlement.overridden` (records `admin_id`, `reason`) |

**Note the asymmetry:** `past_due → grace` is a 14-day window during which nothing changes publicly. `grace → expired` is a 30-day window during which the listing is already downgraded. Cancellation skips grace entirely - they chose to leave, and grace exists for involuntary failure.

### 5.4 Downgrade behaviour

At `expired`, the listing falls back to the **rank-0 tier**. It is never unpublished as a consequence of non-payment.

Unpublishing destroys an indexed page you spent months getting ranked and reads as punitive to the owner. The listing stays; the perks stop.

**Exception:** if the tenant has defined no rank-0 tier - a directory where every listing is paid - an expired listing becomes `visibility: hidden`. This is why "is there a free tier" is a tenant-level setting with real consequences, and the admin UI should say so at the point of configuration.

**Data collected while paid is retained, access gated.** Leads, reviews, analytics and media are never deleted on downgrade. If they re-upgrade, everything is there. The owner dashboard shows a locked-state summary - "you received 34 leads while on Featured" - which is also the most effective renewal prompt you have.

### 5.5 Public page rendering by state

The bug this table exists to prevent: a listing rendering as Featured while the card has been declining for three weeks.

| Status | Badge | Featured placement | Perks (gallery, links, description) | Owner sees |
|---|---|---|---|---|
| `trialing` | Tier badge | Yes | Full | "Trial ends in N days" |
| `active` | Tier badge | Yes | Full | Normal |
| `past_due` | Tier badge | **Yes** | **Full** | Persistent banner: "Payment failed, update card" |
| `grace` | None | No | Rank-0 only | Banner: "Your listing has been downgraded. Restore it." |
| `expired` | None | No | Rank-0 only | Upgrade prompt with locked-state stats |
| `canceled` (pre-period-end) | Tier badge | Yes | Full | "Cancelled, active until {date}" |
| `comped` | Tier badge | Yes | Full | Nothing indicating comp status |

`past_due` keeping full perks is deliberate. Most failed payments are involuntary - an expired card, not a decision to churn. Publicly demoting someone whose card expired is invisible to them and loses customers who intended to pay.

### 5.6 Premium slots

Applies to any tier with `uses_slot: true`.

#### Capacity

A **slot pool** is defined per scope, where scope is a category, a location, a category × location pair, or global. Capacity is configurable per pool in the admin UI.

```jsonc
{
  "id": "pool_01JBQ...",
  "scope": { "type": "category_location", "category": "plumbers", "locality": "Lakeview" },
  "tier": "featured",
  "capacity": 3,
  "locked": 1,                  // held back, not sellable
  "default_listing_id": "listing_01JBQ..."   // shown in unsold slots, nullable
}
```

Sellable capacity is `capacity - locked - occupied`.

#### Slot lifecycle

```
available ──checkout starts──► held (TTL 15 min)
    ▲                              │
    │                       payment succeeds
    │                              ▼
    └──────released────────── occupied ──term ends──► releasing (T-10d) ──► available
                 ▲                  │
                 └──── refund ──────┘
```

`held` is the concurrency control. Two buyers racing for the last slot: the first to start checkout takes an atomic hold; the second is told immediately that the slot is taken rather than after payment. Holds expire after 15 minutes and return to `available`.

#### Unsold slot behaviour

Priority order for filling a slot with no paying occupant:

1. **Locked** - the slot is not filled and not sellable. Admin is reserving inventory or artificially constraining supply.
2. **Default featured** - `default_listing_id` is shown whenever the slot is unsold. Always the same listing.
3. **Rotation** - random selection per page load from eligible listings in scope. This is the fallback when neither of the above is set.

Rotation matters more than it looks: it means a new directory never displays empty featured slots, and free listings get intermittent premium placement, which is the most effective upgrade pitch you have.

#### Terms and renewal

Slot-backed tiers are sold in fixed terms - 30, 60, 90 or 365 days - configurable per pool. `billing_mode` may be `term` (one-time, expires) or `recurring` (auto-renews).

**The incumbent holds right of first refusal until the moment of expiry.** A waitlist entrant cannot pre-empt a sitting occupant.

#### Waitlist

```
T-10 days   slot.waitlist_notified   → "A Featured slot in {scope} may become available on {date}"
            entitlement.renewal_due  → incumbent notified, renewal window open
T-0         slot.released            → slot becomes purchasable, first completed checkout wins
```

The T-10 notice is worded as *may* become available, because the incumbent may renew. If they do, waitlist members receive `slot.waitlist_cleared` - "the slot was renewed, you remain on the list." Overstating availability here produces angry emails; the wording is part of the spec, not a copy decision.

No slot is reserved for a waitlist member. Notification is an equal starting gun; the hold mechanism resolves the race.

#### Slot events

| Type | Notable data |
|---|---|
| `slot.held` | `pool_id`, `listing_id`, `expires_at` |
| `slot.hold_released` | `reason` (`expired` \| `abandoned` \| `payment_failed`) |
| `slot.occupied` | `pool_id`, `listing_id`, `term_days`, `ends_at` |
| `slot.released` | `pool_id`, `previous_listing_id`, `reason` |
| `slot.waitlist_joined` | `pool_id`, `user_id`, `listing_id` |
| `slot.waitlist_notified` | `pool_id`, `recipient_count`, `expected_available_at` |
| `slot.waitlist_cleared` | `pool_id`, `reason` (`renewed` \| `sold`) |
| `slot.capacity_changed` | `pool_id`, `from`, `to`, `changed_by` |

### 5.7 Trials

Trials are enabled per tier, per tenant. **Card required up front.** No-card trials fill a directory with abandoned half-upgrades and convert poorly; card-up-front converts materially better and produces a clean `trialing → active` transition.

If a trial-tier listing occupies a slot, **the slot is held for the trial duration**. A trial that consumes scarce inventory for free needs to be a deliberate admin choice, so trials on slot-backed tiers are configurable separately and default to off.

### 5.8 Admin comps

An admin may grant any tier with no payment attached, with or without an expiry date.

- `comp.expires_at: null` runs indefinitely.
- Comped listings on slot-backed tiers **consume sellable capacity**. Give them a locked slot if you do not want that.
- Every grant emits `entitlement.overridden` with `admin_id` and `reason`. Comps are the single easiest thing to abuse and the audit trail is the control.
- The public page shows a normal tier badge. Nothing indicates comp status.

### 5.9 Annual plans and proration

Annual terms are supported as `term_days: 365` or a recurring annual subscription.

**Core does not compute proration.** A mid-term upgrade is handled entirely by the payment adapter, which reports the outcome; core receives a new `tier` and `current_period_end` and applies them. This keeps a genuinely fiddly piece of arithmetic in the system that already solves it and out of the entitlement engine.

### 5.10 Entitlement events

| Type | Notable data |
|---|---|
| `entitlement.started` | `tier`, `billing_mode`, `period_end`, `trial_ends_at` |
| `entitlement.trial_converted` | `tier`, `period_end` |
| `entitlement.dunning_started` | `attempt`, `dunning_ends_at`, `failure_code` |
| `entitlement.recovered` | `days_in_dunning` |
| `entitlement.downgraded` | `from_tier`, `to_tier`, `grace_ends_at` |
| `entitlement.restored` | `tier`, `days_in_grace` |
| `entitlement.renewal_due` | `days_remaining`, `term_days` |
| `entitlement.expired` | `from_tier`, `cause` |
| `entitlement.canceled` | `at_period_end`, `reason`, `canceled_by` |
| `entitlement.overridden` | `admin_id`, `reason`, `from`, `to` |

---

## 6. Commands (adapter → core)

```
listing.upsert          listing.setVisibility    listing.merge
listing.attachMedia
claim.submit            claim.approve            claim.reject
lead.create             lead.markSpam
review.submit           review.respond           review.flag
entitlement.grant       entitlement.revoke       entitlement.reportPayment
slot.hold               slot.release
consent.record
moderation.enqueue      moderation.decide
```

**`listing.setTier` is removed from the command surface.** Tier is derived from entitlement, not set directly. A payment adapter calls `entitlement.reportPayment`; core decides the tier consequence. Admin overrides go through `entitlement.grant`.

```json
{
  "command": "entitlement.reportPayment",
  "idempotency_key": "stripe:evt_1QxYz",
  "tenant_id": "tnt_01JBQ2K9",
  "adapter_id": "stripe",
  "trace_id": "01JBQ7X2M4K8ZP3RVN6T9WGYHD",
  "payload": {
    "listing_id": "listing_01JBQ...",
    "outcome": "succeeded",
    "tier": "featured",
    "period_end": "2026-09-28T00:00:00Z",
    "external_id": "sub_1QxYz"
  }
}
```

Core responds `202` with the resulting event ID, `409` on idempotency replay carrying the original event ID, or `422` with a validation problem document. **Adapters must treat `409` as success.**

Derive `idempotency_key` from the external system's identifiers so a webhook redelivery collapses to one effect.

**Every command is logged, including rejected and blocked ones** (§10.2).

---

## 7. Adapter interface

```ts
export type Capability =
  | "email.send"
  | "sms.send"
  | "voice.call"
  | "postal.send"            // physical mail, for postcard verification
  | "crm.sync_contact"
  | "payments.checkout"
  | "payments.subscription"
  | "media.store"
  | "agent.converse"
  | "analytics.track"
  | "reviews.fetch"          // OPTIONAL - external review display, §4.1
  | "search.index";          // OPTIONAL upgrade - core search always works without it

export type Scope =
  | "pii:contact"
  | "pii:message"
  | "command:listing"
  | "command:claim"
  | "command:entitlement"
  | "command:moderation";

export interface AdapterManifest {
  id: string;                    // "stripe" - stable, lowercase, no vendor version
  name: string;
  version: string;
  osds_api: string;              // semver range, e.g. "^1.0.0"
  bundled?: boolean;             // ships in-repo; see §7.6
  default_enabled?: boolean;     // enabled on a fresh install
  capabilities: Capability[];
  scopes: Scope[];
  subscribes: string[];          // ["claim.*", "listing.tier_changed"]
  config_schema: JSONSchema7;
  secrets: string[];
  inbound_routes?: string[];
  egress_allowlist: string[];
  homepage?: string;
  license: string;
}

export interface AdapterContext {
  tenant: { id: string; slug: string; domain: string | null };
  config: Record<string, unknown>;
  secrets: SecretResolver;
  command: CommandClient;
  logger: Logger;                // auto-redacts declared PII fields
  kv: KeyValueStore;             // adapter-scoped and tenant-scoped
  http: FetchLike;               // instrumented, egress-allowlisted
  clock: () => Date;
}

export type HandleResult =
  | { status: "ok"; note?: string }
  | { status: "skipped"; reason: string }
  | { status: "retry"; after_ms: number; reason: string }
  | { status: "failed"; reason: string; permanent: true };

export interface Adapter {
  manifest: AdapterManifest;
  init?(ctx: AdapterContext): Promise<void>;
  handle(event: OsdsEvent, ctx: AdapterContext): Promise<HandleResult>;
  actions?: Record<string, (input: unknown, ctx: AdapterContext) => Promise<unknown>>;
  inbound?(req: InboundRequest, ctx: AdapterContext): Promise<InboundResult>;
  health?(ctx: AdapterContext): Promise<{ ok: boolean; detail?: string }>;
}
```

### 7.1 Config and secrets hierarchy

**Config is per-tenant.** An operator running ten directories configures each one separately, because pipeline IDs and price IDs differ.

**Secrets resolve deployment-level first, with per-tenant override:**

```
secrets.get("stripe_key")
  → tenant override, if set
  → deployment-level value, if set
  → throw ConfigurationError
```

A self-hoster sets one SMTP password once and every tenant inherits it. Secrets never appear in `config`, never appear in event payloads, and are redacted by `ctx.logger` unconditionally.

### 7.2 Delivery semantics

- At-least-once, dedupe on `event.id`.
- Ordering guaranteed per `subject`, not globally.
- Retry: exponential, jittered, `1s → 2s → 4s → … → 1h`, 12 attempts.
- Exhaustion lands in the tenant DLQ with the full envelope, replayable from the admin UI.
- Handler timeout 30s.
- `failed` with `permanent: true` skips retries.

### 7.3 What adapters must not do

- Emit events. Only core emits.
- Handle events they originated (`origin === manifest.id`).
- Write `tier` directly. Report payment outcomes; core decides.
- Log values from `contact`, `consent`, or message bodies.
- Store OSDS primary keys as their own source of truth. Map external IDs in `ctx.kv`.
- Contact hosts outside `egress_allowlist`.

### 7.4 Non-TypeScript adapters

`GET {base_url}/manifest` returns the manifest. `POST {base_url}/events` receives the event with `X-OSDS-Signature` (HMAC-SHA256 over the raw body, key rotated per install). Responses: `2xx` ok · `409` duplicate · `429` with `Retry-After` · `5xx` retry.

### 7.5 Agent restrictions - enforced by scope, not by prompt

Agents live outside OSDS. Core defines and enforces what they may do; the adapter implements the conversation. If the agent platform changes, the guardrails stay.

- No `command:entitlement`. An agent may read state and *request* a refund, which lands in `moderation.queued`.
- No `compliance.*` commands.
- No listing deletion.
- Outbound message rate caps per tenant per hour, with a global kill switch flipping every agent scope to read-only.
- Every `agent.action_taken` carries a resolvable `transcript_ref`. No transcript, no action.
- Bot disclosure is enforced at adapter registration, not a per-tenant setting an operator can switch off.

**Mandatory escalation triggers.** Core will not execute an agent command past any of these:

`legal_threat` · `refund_or_chargeback` · `payment_dispute` · `removal_request` · `defamation_claim` · `three_failed_resolutions` · `sentiment_below_threshold` · `identity_verification_override` · `bulk_action_over_threshold`

### 7.6 Bundled adapters

Rule 1 says core never imports adapter code. But a system with zero adapters cannot send a claim verification code, which means nobody can claim a listing. Transactional email is load-bearing, not optional.

Resolved by shipping adapters in-repo that are still adapters:

| Adapter | Bundled | Default enabled | Why |
|---|---|---|---|
| `smtp` | Yes | **Yes** | Configured in the first-run wizard. Without it, claims cannot complete. |
| `webhook` | Yes | **Yes** | POSTs any event to a URL. The universal escape hatch for n8n, Make, or anything else. |
| `stripe` | Yes | No | Reference payments implementation. Enabled only if the operator sells anything. |
| `paypal` | Yes | No | Second payments provider, proves the payments capability is not Stripe-shaped. |
| `gohighlevel` | Yes | No | Reference CRM implementation (§9). |

Core still knows only the capability names. `smtp` is imported by the *bootstrap configuration*, not by core logic - the distinction that keeps rule 1 intact.

**A directory with no payment adapter is fully functional**, running free tiers only. The admin UI hides purchasable tiers when no `payments.checkout` capability is available, rather than presenting a checkout that cannot complete.

---

## 8. Claim verification

Verification methods are enabled per tenant in the setup wizard. **At least one must be enabled; manual admin review is the default and is always available as a fallback.**

| Method | Strength | Requires | Notes |
|---|---|---|---|
| `manual` | Varies | Nothing | **Default.** Always available. |
| `phone_otp` | Strong | `sms.send` or `voice.call` adapter | The workhorse. Proves control of the listed line. |
| `domain_email` | Weak | `email.send` (bundled) | Only meaningful when the listing has a real company domain. Useless for the large share of small businesses on free mail. |
| `gbp_oauth` | Strongest | Google API access | Optional, never default. See §8.1. |
| `postcard` | Strong for address | `postal.send` adapter | See §8.2. |

### 8.1 Google Business Profile OAuth

The strongest signal available: the claimant proves they already manage the Google Business Profile for that location.

The practical obstacle for an open source project is that Google Business Profile API access requires a separate approved Google Cloud project per user of the API. That means **every self-hoster needs their own approved project**, not just the OSDS steward. Approval is not automatic.

Consequences for the design:

- Ships as an optional adapter, never enabled by default.
- The setup wizard explains the approval requirement before the operator invests time.
- Core must function fully when it is absent.
- **Verify the current access process independently before building against it.** This is exactly the kind of requirement that changes.

### 8.2 Postcard verification

A code is printed on a postcard and mailed to the listed address. The claimant enters it. Strong evidence for the address specifically - it proves someone at that physical location received the mail.

Implemented as a `postal.send` capability adapter. Print-and-mail APIs exist as a commercial service category (Lob and PostGrid are two; verify current pricing and availability yourself). Expect roughly a dollar or two per piece and a physical delivery time of several days.

Design parameters:

- Code is 6 digits, valid **21 days**, single use.
- One postcard per listing per 30 days, rate-limited to prevent an attacker generating mail volume at the operator's expense.
- Emits `claim.verification_started` with `method: "postcard"` and `expires_at`, then `postal.dispatched` when the adapter confirms handoff.
- **Cost falls on the operator**, so the admin UI must show per-piece cost at the point of enabling it. An operator who turns this on without noticing will be unhappy.

### 8.3 Manual verification recording

When an admin verifies a claim by hand, the record must capture how - not just that it happened.

```jsonc
// claim.approved data, manual path
{
  "claim": { "id": "claim_01JBQ...", "listing_id": "listing_01JBQ...", "method": "manual" },
  "manual_verification": {
    "method_used": "phone",          // phone | email | postcard | website | social | in_person | document | other
    "verified_by": "usr_admin_01JBQ...",
    "verified_at": "2026-08-28T16:04:00Z",
    "notes": "Called listed number, spoke with Dana Hoffman, confirmed ownership.",
    "evidence_ref": null             // optional pointer to an uploaded document
  }
}
```

This is the record you produce when a claim is disputed six months later. `notes` is required, not optional - an admin who cannot articulate how they verified something has not verified it.

### 8.4 Anti-hijack measures

A competitor or ex-employee claiming a listing takes control of that business's public page. Three mandatory mitigations:

**Notify every existing contact channel on successful claim.** Email and SMS to whatever was already on the listing: "your listing was just claimed - if this wasn't you, click here." This catches what verification misses and costs almost nothing. Emits `claim.notified_existing_contacts`.

**Never leak contact details through the verification UI.** "We'll text +1 (773) 555-0142" turns the claim flow into a phone-number disclosure endpoint for every scraped listing on the site. Mask it: `+1 (773) •••-•142`, `d•••@hoffmanplumbing.example`. This is missed constantly.

**Disputes go to moderation, never auto-transfer.** A second claim on an already-claimed listing opens a `moderation.queued` item. Verification alone never moves ownership away from a sitting owner. Rate-limit claim attempts per IP and per account.

---

## 9. Reference adapter: `gohighlevel`

Shipped in-repo as the worked example and as the thing that runs the steward's own directories. **Nothing in core knows it exists, and the entire system functions without it.**

**Config (per tenant):** `location_id`, `pipeline_id`, `claim_workflow_webhook_url`, `tier_field_key`, `tag_prefix` (default `osds`)
**Secrets:** `ghl_private_integration_token`
**Scopes:** `pii:contact`, `command:listing`
**Egress allowlist:** `services.leadconnectorhq.com`

| OSDS event | GHL action |
|---|---|
| `claim.submitted` | Contacts **upsert** (email lowercased, phone E.164) → tag `osds:claim-pending` → set `osds_listing_id`, `osds_tenant` → write consent fields → POST to `claim_workflow_webhook_url` |
| `claim.approved` | Retag `osds:owner` → create Opportunity in `pipeline_id`, stage *Claimed* |
| `claim.verification_failed` | Tag `osds:claim-stalled`; a GHL workflow owns the nudge sequence |
| `listing.tier_changed` | Update `osds_tier` custom field, move Opportunity stage |
| `entitlement.dunning_started` | Tag `osds:dunning`, fire dunning workflow webhook |
| `entitlement.downgraded` | Tag `osds:downgraded`, start win-back sequence |
| `slot.waitlist_notified` | Tag `osds:waitlist-active` |
| `lead.captured` | Post as a Conversation note on the owner's contact |
| `agent.escalation_requested` | Create a Task assigned to a human, tag `osds:escalated`, remove `osds:ai-active` |

**Rate limiting.** GHL API v2 documents a burst limit of 100 requests per 10 seconds and 200,000 per day, per app per location. Normal traffic never approaches this; a 10,000-listing backfill does. The adapter reads `X-RateLimit-Remaining` and `X-RateLimit-Daily-Remaining` on every response and throttles proactively rather than reacting to 429s.

**Loop guard.** Every write carries the `osds` tag prefix; the inbound handler drops any GHL webhook whose change originated from a tagged write.

**Deliberately not in this adapter:** listing storage. GHL Contacts represent *people*. The listing lives in OSDS. `osds_listing_id` on a contact is a pointer, never a copy.

---

## 10. Transport, logging and retention

### 10.1 Transport: Postgres outbox

Core writes events to an `outbox` table in the same transaction as the state change. A worker consumes it, using `LISTEN/NOTIFY` for latency with a polling fallback for reliability.

No message broker, no extra container, survives restarts, inspectable with SQL. A directory does not generate broker-scale traffic. The transport sits behind an interface so it can be swapped later, but that must never become a requirement for self-hosters.

### 10.2 Three logs, three retentions

| Log | Contents | Retention |
|---|---|---|
| **Event log** | Things that happened | Envelope **forever**; `data` payload **90 days**, then nulled |
| **Command log** | Things *attempted*, including rejected and blocked | **Forever**, payload nulled at 90 days |
| **Access log** | Who viewed or exported what | **2 years**, separate store |

The envelope is small and free of personal data, so keeping it indefinitely is a cheap permanent audit trail. The payload holds phone numbers, emails and message bodies - a second copy of personal data with its own retention obligation, and the copy people forget when processing a deletion request. Nulling at 90 days keeps the debugging value without accumulating a shadow PII database. Replay older than 90 days reconstructs from current state.

The command log exists because a rejected command previously left no trace. "The agent attempted to delete a listing and was blocked" is precisely the record worth having.

---

## 11. Search, sitemaps and SEO

### 11.1 Search is core

Required, always present, working on a fresh install with no configuration and no extra container:

- **Postgres full-text** for name, description and category
- **pg_trgm** for fuzzy matching and typo tolerance
- **PostGIS** for radius and bounding-box queries

The `search.index` capability remains an optional upgrade for operators wanting Meilisearch, Typesense or Elasticsearch. A default deployment must never produce a directory that cannot be searched.

### 11.2 Sitemap index from day one

The sitemap protocol caps a single file at 50,000 URLs and 50MB. Implement a sitemap **index** immediately, pointing at one child file, sharding automatically past the threshold. It is a small amount of code and means never revisiting it.

Worth doing even for directories that will never hold 50,000 listings, because **URL count grows much faster than listing count**. Categories × locations × pagination can produce far more URLs than there are listings. A 5,000-listing directory with faceted browse crosses 50,000 URLs comfortably.

That same arithmetic is a warning: decide deliberately which facet combinations are indexable and which carry `noindex`. Generating tens of thousands of thin combination pages is a well-known way to damage a site's standing in search rather than improve it.

---

## 12. Deployment model

Target: a semi-technical operator who can rent a server but should never edit YAML to set an admin password.

```
osds-app       (web + admin + API)
osds-worker    (outbox consumer, scheduled jobs, adapter runtime)
postgres       (bundled; overridable via DATABASE_URL)
minio          (bundled S3-compatible storage; overridable via S3_* vars)
```

Two OSDS containers, two infrastructure containers, both infrastructure containers replaceable by managed services through environment variables alone.

**Distribution:** `docker-compose.yml` as the reference path; one-click templates for Railway, Render and Coolify; marketplace images for DigitalOcean, AWS and Azure where genuinely one-click; Helm chart later if anyone asks.

**First-run wizard, not config files.** Admin account, site name, domain, single-vs-multi-directory mode, SMTP, and enabled claim verification methods are all set in the browser on first boot. Environment variables exist for people who want them and are never required.

**Single-directory mode** hides the tenant selector and scopes the UI to one tenant. Every table still carries `tenant_id`; switching to multi-directory later is a settings change, not a migration.

**Scheduled jobs** the worker must run: dunning transitions, grace expiry, term expiry, `T-10` renewal and waitlist notifications, slot hold expiry, payload nulling at 90 days, sitemap regeneration.

---

## 13. Versioning

- `version` is per event type. Additive changes do not bump it.
- Breaking changes emit both versions in parallel for one minor release cycle.
- Adapters declare `osds_api` as a semver range; core refuses to load an out-of-range adapter rather than failing at runtime.
- Event type names are permanent.

---

## 14. Still open

1. **Adapter test harness** - a fixture event stream a third-party adapter can self-certify against before submission. Needed before any external adapter is accepted.
2. **Media pipeline** - resize, EXIF strip, format conversion, abuse scanning, and where it sits relative to `media.store`.
3. **Data model and migrations** - the entitlement and slot tables in §5 are specified behaviourally but not yet as schema.
4. **Owner dashboard scope** - what an owner can edit without re-verification, and what changes re-open moderation.
5. **Import pipeline detail** - CSV column mapping, dedupe strategy against `suppression_key`, batch rollback mechanics.
6. **Rate limiting and abuse** - public API limits, claim attempt limits, review submission limits.
