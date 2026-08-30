/**
 * The entitlement state machine - spec §6.2 (states) and §6.3 (transitions).
 *
 * Pure. No database, no clock, no I/O. Given the current status and a trigger it
 * returns the next status and the events the transition emits, or throws
 * {@link IllegalTransitionError} - illegal transitions are rejected, never
 * silently ignored (§6.3).
 *
 * Time- and money-derived facts (dunning windows, `period_end`, proration) are
 * the caller's job: it establishes that a trigger's precondition holds and fires
 * the trigger. This module only encodes the table.
 */
import type { EntitlementEventType, ListingEventType } from "@osds/adapter-kit";

/** §6.2. `none` means "no entitlement, listing sits on the rank-0 tier". */
export type EntitlementStatus =
  | "none"
  | "trialing"
  | "active"
  | "past_due"
  | "grace"
  | "expired"
  | "canceled"
  | "comped";

const STATUSES: ReadonlySet<EntitlementStatus> = new Set([
  "none",
  "trialing",
  "active",
  "past_due",
  "grace",
  "expired",
  "canceled",
  "comped",
]);

/**
 * The triggers named in the §6.3 "Trigger" column. Each carries only what the
 * table branches on - `checkout_completed` on whether a trial is configured,
 * `trial_ended` on whether the charge succeeded, `admin_override` on its target.
 */
export type EntitlementTrigger =
  | { readonly type: "checkout_completed"; readonly trial: boolean }
  | { readonly type: "trial_ended"; readonly paymentSucceeded: boolean }
  | { readonly type: "owner_canceled" }
  | { readonly type: "payment_failed" }
  | { readonly type: "refund_issued" }
  | { readonly type: "term_ended" }
  | { readonly type: "payment_succeeded" }
  | { readonly type: "dunning_exhausted" }
  | { readonly type: "grace_expired" }
  | { readonly type: "period_ended" }
  | { readonly type: "comp_expired" }
  | {
      readonly type: "admin_override";
      readonly to: EntitlementStatus;
      readonly adminId: string;
      readonly reason: string;
    };

export type EntitlementTriggerType = EntitlementTrigger["type"];

/**
 * Event types a §6.3 transition can emit: the §6.10 entitlement events except
 * `entitlement.renewal_due` (a scheduled notification, not a transition), plus
 * `listing.tier_changed`.
 */
export type EmittedEventType =
  | Exclude<EntitlementEventType, "entitlement.renewal_due">
  | Extract<ListingEventType, "listing.tier_changed">;

export interface TransitionResult {
  readonly from: EntitlementStatus;
  readonly to: EntitlementStatus;
  readonly emits: readonly EmittedEventType[];
}

export class IllegalTransitionError extends Error {
  constructor(
    readonly from: EntitlementStatus,
    readonly trigger: EntitlementTrigger,
  ) {
    super(`no entitlement transition from "${from}" for trigger "${trigger.type}"`);
    this.name = "IllegalTransitionError";
  }
}

function ok(
  from: EntitlementStatus,
  to: EntitlementStatus,
  emits: readonly EmittedEventType[],
): TransitionResult {
  return { from, to, emits };
}

/**
 * Apply one §6.3 transition. Throws {@link IllegalTransitionError} when the
 * (status, trigger) pair is not in the table.
 */
export function transition(
  from: EntitlementStatus,
  trigger: EntitlementTrigger,
): TransitionResult {
  switch (trigger.type) {
    // none | Checkout completes, trial configured | trialing
    // none | Checkout completes, no trial         | active
    case "checkout_completed":
      if (from === "none") {
        return trigger.trial
          ? ok(from, "trialing", ["entitlement.started", "listing.tier_changed"])
          : ok(from, "active", ["entitlement.started", "listing.tier_changed"]);
      }
      break;

    // trialing | trial_ends_at reached, payment succeeds | active   | entitlement.trial_converted
    // trialing | trial_ends_at reached, payment fails    | past_due | entitlement.dunning_started
    case "trial_ended":
      if (from === "trialing") {
        return trigger.paymentSucceeded
          ? ok(from, "active", ["entitlement.trial_converted"])
          : ok(from, "past_due", ["entitlement.dunning_started"]);
      }
      break;

    // trialing | Owner cancels | canceled | entitlement.canceled
    // active   | Owner cancels | canceled | entitlement.canceled
    case "owner_canceled":
      if (from === "trialing" || from === "active") {
        return ok(from, "canceled", ["entitlement.canceled"]);
      }
      break;

    // active | billing.payment_failed | past_due | entitlement.dunning_started
    case "payment_failed":
      if (from === "active") {
        return ok(from, "past_due", ["entitlement.dunning_started"]);
      }
      break;

    // active | billing.refund_issued | expired | listing.tier_changed (immediate)
    case "refund_issued":
      if (from === "active") {
        return ok(from, "expired", ["listing.tier_changed"]);
      }
      break;

    // active | Term ends, billing_mode=term, not renewed | expired | entitlement.expired, listing.tier_changed
    case "term_ended":
      if (from === "active") {
        return ok(from, "expired", ["entitlement.expired", "listing.tier_changed"]);
      }
      break;

    // past_due | Payment succeeds | active | entitlement.recovered
    // grace    | Owner pays       | active | entitlement.restored, listing.tier_changed
    case "payment_succeeded":
      if (from === "past_due") {
        return ok(from, "active", ["entitlement.recovered"]);
      }
      if (from === "grace") {
        return ok(from, "active", ["entitlement.restored", "listing.tier_changed"]);
      }
      break;

    // past_due | 14 days elapsed | grace | entitlement.downgraded, listing.tier_changed
    case "dunning_exhausted":
      if (from === "past_due") {
        return ok(from, "grace", ["entitlement.downgraded", "listing.tier_changed"]);
      }
      break;

    // grace | 30 days elapsed | expired | entitlement.expired
    // (no listing.tier_changed: at grace the listing was already downgraded)
    case "grace_expired":
      if (from === "grace") {
        return ok(from, "expired", ["entitlement.expired"]);
      }
      break;

    // canceled | current_period_end reached | expired | entitlement.expired, listing.tier_changed
    case "period_ended":
      if (from === "canceled") {
        return ok(from, "expired", ["entitlement.expired", "listing.tier_changed"]);
      }
      break;

    // comped | comp.expires_at reached | expired | entitlement.expired
    case "comp_expired":
      if (from === "comped") {
        return ok(from, "expired", ["entitlement.expired"]);
      }
      break;

    // any | Admin override | any | entitlement.overridden (records admin_id, reason)
    case "admin_override":
      if (STATUSES.has(trigger.to)) {
        return ok(from, trigger.to, ["entitlement.overridden"]);
      }
      break;
  }

  throw new IllegalTransitionError(from, trigger);
}
