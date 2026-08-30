import { describe, it, expect } from "vitest";
import {
  transition,
  IllegalTransitionError,
  type EntitlementStatus,
  type EntitlementTrigger,
} from "./entitlement.js";

const ALL_STATUSES: readonly EntitlementStatus[] = [
  "none",
  "trialing",
  "active",
  "past_due",
  "grace",
  "expired",
  "canceled",
  "comped",
];

/**
 * One assertion per row of the §6.3 transition table: (from, trigger) ->
 * { from, to, emits }, in table order.
 */
describe("§6.3 transitions", () => {
  it("none + checkout completes, trial configured -> trialing", () => {
    expect(transition("none", { type: "checkout_completed", trial: true })).toEqual({
      from: "none",
      to: "trialing",
      emits: ["entitlement.started", "listing.tier_changed"],
    });
  });

  it("none + checkout completes, no trial -> active", () => {
    expect(transition("none", { type: "checkout_completed", trial: false })).toEqual({
      from: "none",
      to: "active",
      emits: ["entitlement.started", "listing.tier_changed"],
    });
  });

  it("trialing + trial_ends_at reached, payment succeeds -> active", () => {
    expect(transition("trialing", { type: "trial_ended", paymentSucceeded: true })).toEqual({
      from: "trialing",
      to: "active",
      emits: ["entitlement.trial_converted"],
    });
  });

  it("trialing + trial_ends_at reached, payment fails -> past_due", () => {
    expect(transition("trialing", { type: "trial_ended", paymentSucceeded: false })).toEqual({
      from: "trialing",
      to: "past_due",
      emits: ["entitlement.dunning_started"],
    });
  });

  it("trialing + owner cancels -> canceled", () => {
    expect(transition("trialing", { type: "owner_canceled" })).toEqual({
      from: "trialing",
      to: "canceled",
      emits: ["entitlement.canceled"],
    });
  });

  it("active + billing.payment_failed -> past_due", () => {
    expect(transition("active", { type: "payment_failed" })).toEqual({
      from: "active",
      to: "past_due",
      emits: ["entitlement.dunning_started"],
    });
  });

  it("active + owner cancels -> canceled", () => {
    expect(transition("active", { type: "owner_canceled" })).toEqual({
      from: "active",
      to: "canceled",
      emits: ["entitlement.canceled"],
    });
  });

  it("active + billing.refund_issued -> expired (tier change only, immediate)", () => {
    expect(transition("active", { type: "refund_issued" })).toEqual({
      from: "active",
      to: "expired",
      emits: ["listing.tier_changed"],
    });
  });

  it("active + term ends, not renewed -> expired", () => {
    expect(transition("active", { type: "term_ended" })).toEqual({
      from: "active",
      to: "expired",
      emits: ["entitlement.expired", "listing.tier_changed"],
    });
  });

  it("past_due + payment succeeds -> active", () => {
    expect(transition("past_due", { type: "payment_succeeded" })).toEqual({
      from: "past_due",
      to: "active",
      emits: ["entitlement.recovered"],
    });
  });

  it("past_due + 14 days elapsed -> grace", () => {
    expect(transition("past_due", { type: "dunning_exhausted" })).toEqual({
      from: "past_due",
      to: "grace",
      emits: ["entitlement.downgraded", "listing.tier_changed"],
    });
  });

  it("grace + owner pays -> active", () => {
    expect(transition("grace", { type: "payment_succeeded" })).toEqual({
      from: "grace",
      to: "active",
      emits: ["entitlement.restored", "listing.tier_changed"],
    });
  });

  it("grace + 30 days elapsed -> expired (no tier change: already downgraded)", () => {
    expect(transition("grace", { type: "grace_expired" })).toEqual({
      from: "grace",
      to: "expired",
      emits: ["entitlement.expired"],
    });
  });

  it("canceled + current_period_end reached -> expired", () => {
    expect(transition("canceled", { type: "period_ended" })).toEqual({
      from: "canceled",
      to: "expired",
      emits: ["entitlement.expired", "listing.tier_changed"],
    });
  });

  it("comped + comp.expires_at reached -> expired", () => {
    expect(transition("comped", { type: "comp_expired" })).toEqual({
      from: "comped",
      to: "expired",
      emits: ["entitlement.expired"],
    });
  });

  it("any + admin override -> any (records admin_id, reason)", () => {
    expect(
      transition("active", {
        type: "admin_override",
        to: "comped",
        adminId: "usr_admin_1",
        reason: "goodwill",
      }),
    ).toEqual({
      from: "active",
      to: "comped",
      emits: ["entitlement.overridden"],
    });
  });
});

describe("§6.3 admin override is legal from every state", () => {
  for (const from of ALL_STATUSES) {
    it(`${from} -> comped via admin override`, () => {
      expect(
        transition(from, {
          type: "admin_override",
          to: "comped",
          adminId: "usr_admin_1",
          reason: "test",
        }),
      ).toEqual({ from, to: "comped", emits: ["entitlement.overridden"] });
    });
  }

  it("rejects an override to a value that is not a status", () => {
    expect(() =>
      transition("active", {
        type: "admin_override",
        // deliberately invalid target
        to: "bogus" as EntitlementStatus,
        adminId: "usr_admin_1",
        reason: "test",
      }),
    ).toThrow(IllegalTransitionError);
  });
});

describe("illegal transitions are rejected, not silently ignored", () => {
  const cases: ReadonlyArray<readonly [EntitlementStatus, EntitlementTrigger]> = [
    ["none", { type: "payment_failed" }],
    ["none", { type: "owner_canceled" }],
    ["active", { type: "checkout_completed", trial: false }],
    ["active", { type: "grace_expired" }],
    ["active", { type: "payment_succeeded" }],
    ["trialing", { type: "payment_failed" }],
    ["past_due", { type: "term_ended" }],
    ["grace", { type: "dunning_exhausted" }],
    ["comped", { type: "payment_failed" }],
    ["expired", { type: "payment_succeeded" }],
    ["expired", { type: "owner_canceled" }],
    ["canceled", { type: "payment_succeeded" }],
  ];

  for (const [from, trigger] of cases) {
    it(`${from} + ${trigger.type} throws`, () => {
      expect(() => transition(from, trigger)).toThrow(IllegalTransitionError);
      try {
        transition(from, trigger);
      } catch (err) {
        expect(err).toBeInstanceOf(IllegalTransitionError);
        expect((err as IllegalTransitionError).from).toBe(from);
        expect((err as IllegalTransitionError).trigger).toEqual(trigger);
      }
    });
  }
});

describe("the full (status x trigger) matrix has no silent gaps", () => {
  const sampleTriggers: readonly EntitlementTrigger[] = [
    { type: "checkout_completed", trial: true },
    { type: "checkout_completed", trial: false },
    { type: "trial_ended", paymentSucceeded: true },
    { type: "trial_ended", paymentSucceeded: false },
    { type: "owner_canceled" },
    { type: "payment_failed" },
    { type: "refund_issued" },
    { type: "term_ended" },
    { type: "payment_succeeded" },
    { type: "dunning_exhausted" },
    { type: "grace_expired" },
    { type: "period_ended" },
    { type: "comp_expired" },
    { type: "admin_override", to: "active", adminId: "usr_x", reason: "m" },
  ];

  const key = (t: EntitlementTrigger): string =>
    t.type === "checkout_completed"
      ? `checkout_completed:${t.trial}`
      : t.type === "trial_ended"
        ? `trial_ended:${t.paymentSucceeded}`
        : t.type;

  // Legal (from -> trigger keys), transcribed from §6.3. admin_override is legal
  // from every state and handled separately.
  const legal: Readonly<Record<EntitlementStatus, readonly string[]>> = {
    none: ["checkout_completed:true", "checkout_completed:false"],
    trialing: ["trial_ended:true", "trial_ended:false", "owner_canceled"],
    active: ["owner_canceled", "payment_failed", "refund_issued", "term_ended"],
    past_due: ["payment_succeeded", "dunning_exhausted"],
    grace: ["payment_succeeded", "grace_expired"],
    canceled: ["period_ended"],
    comped: ["comp_expired"],
    expired: [],
  };

  for (const from of ALL_STATUSES) {
    for (const trigger of sampleTriggers) {
      const k = key(trigger);
      const expectedLegal = trigger.type === "admin_override" || legal[from].includes(k);

      it(`${from} + ${k} is ${expectedLegal ? "accepted" : "rejected"}`, () => {
        if (expectedLegal) {
          const r = transition(from, trigger);
          expect(r.from).toBe(from);
          expect(ALL_STATUSES).toContain(r.to);
          expect(r.emits.length).toBeGreaterThan(0);
        } else {
          expect(() => transition(from, trigger)).toThrow(IllegalTransitionError);
        }
      });
    }
  }
});
