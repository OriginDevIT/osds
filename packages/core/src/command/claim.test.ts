import { describe, it, expect } from "vitest";
import type { OsdsCommand } from "@osds/adapter-kit";
import {
  handleClaimSubmit,
  handleClaimApprove,
  withClaimId,
  type ClaimListing,
  type ClaimMethod,
  type ClaimRecord,
} from "./claim.js";

// --- fixtures ----------------------------------------------------------

function submitCommand(
  payload: Record<string, unknown>,
  over: Partial<OsdsCommand> = {},
): OsdsCommand {
  return {
    command: "claim.submit",
    idempotency_key: "ghl:evt_1",
    tenant_id: "tnt_x",
    adapter_id: "gohighlevel",
    trace_id: "01JBQ7X2M4K8ZP3RVN6T9WGYHD",
    payload,
    ...over,
  };
}

function approveCommand(
  payload: Record<string, unknown>,
  over: Partial<OsdsCommand> = {},
): OsdsCommand {
  return {
    command: "claim.approve",
    idempotency_key: "admin:approve_1",
    tenant_id: "tnt_x",
    adapter_id: "admin-console",
    trace_id: "01JBQ7X2M4K8ZP3RVN6T9WGYHD",
    payload,
    ...over,
  };
}

const ENABLED: readonly ClaimMethod[] = [
  "manual",
  "phone_otp",
  "domain_email",
  "gbp_oauth",
];

/** Fixed injected clock. phone_otp default TTL is 10 min (§9.5). */
const NOW = new Date("2026-08-28T14:22:10.000Z");
const PHONE_OTP_DEADLINE = "2026-08-28T14:32:10.000Z";

const consent = {
  marketing_email: {
    granted: true,
    at: "2026-08-28T14:22:10.000Z",
    ip: "203.0.113.44",
    text_version: "consent-v3",
  },
  automated_calls: {
    granted: false,
    at: null,
    ip: null,
    text_version: "consent-v3",
  },
};

const claimant = {
  id: "usr_dana",
  name: "Dana Hoffman",
  email: "Dana@Hoffmanplumbing.Example",
  phone_e164: "+17735550142",
  role_claimed: "owner",
};

const unclaimed: ClaimListing = {
  id: "listing_hoffman",
  tenant_id: "tnt_x",
  status: "unclaimed",
};
const claimed: ClaimListing = { ...unclaimed, status: "claimed" };

const validSubmit = {
  listing_id: "listing_hoffman",
  method: "phone_otp",
  claimant,
  consent,
};

const pendingClaim: ClaimRecord = {
  id: "claim_1",
  tenant_id: "tnt_x",
  listing_id: "listing_hoffman",
  status: "verifying",
  method: "phone_otp",
  claimant_user_id: "usr_dana",
};
const manualClaim: ClaimRecord = {
  ...pendingClaim,
  id: "claim_2",
  method: "manual",
};

const manualVerification = {
  method_used: "phone",
  verified_by: "usr_admin",
  verified_at: "2026-08-28T16:04:00.000Z",
  notes: "Called listed number, spoke with Dana, confirmed ownership.",
  evidence_ref: null,
};

// --- claim.submit ----------------------------------------------------

describe("claim.submit", () => {
  it("emits claim.submitted then claim.verification_started for a non-manual method", () => {
    const res = handleClaimSubmit(
      submitCommand(validSubmit),
      unclaimed,
      ENABLED,
      NOW,
    );

    expect(res.outcome).toBe("submitted");
    if (res.outcome !== "submitted") throw new Error("unreachable");

    expect(res.events.map((e) => e.type)).toEqual([
      "claim.submitted",
      "claim.verification_started",
    ]);

    const [submitted, verification] = res.events;
    expect(submitted.subject).toBe("listing_hoffman");
    // The draft carries no claim id yet.
    expect("id" in submitted.data.claim).toBe(false);
    expect(submitted.data.claim).toEqual({
      listing_id: "listing_hoffman",
      status: "pending_verification",
      method: "phone_otp",
    });
    expect(submitted.data.claimant.email).toBe("dana@hoffmanplumbing.example");
    expect(submitted.data.consent).toEqual(consent);
    // §9.5: core computes the deadline from the phone_otp default TTL (10 min).
    expect(verification).toEqual({
      type: "claim.verification_started",
      subject: "listing_hoffman",
      data: { method: "phone_otp", expires_at: PHONE_OTP_DEADLINE },
    });
  });

  it("computes expires_at from an in-bounds tenant TTL override (§9.5)", () => {
    const res = handleClaimSubmit(
      submitCommand(validSubmit),
      unclaimed,
      ENABLED,
      NOW,
      { phone_otp_minutes: 30 },
    );
    if (res.outcome !== "submitted") throw new Error("unreachable");
    expect(res.events[1]?.data).toEqual({
      method: "phone_otp",
      expires_at: "2026-08-28T14:52:10.000Z",
    });
  });

  it("ignores a caller-supplied verification_expires_at", () => {
    const res = handleClaimSubmit(
      submitCommand({
        ...validSubmit,
        verification_expires_at: "2026-08-29T14:22:10.000Z",
      }),
      unclaimed,
      ENABLED,
      NOW,
    );
    if (res.outcome !== "submitted") throw new Error("unreachable");
    expect(res.events[1]?.data).toEqual({
      method: "phone_otp",
      expires_at: PHONE_OTP_DEADLINE,
    });
  });

  it("emits expires_at: null for gbp_oauth (no OSDS-side code, §9.5)", () => {
    const res = handleClaimSubmit(
      submitCommand({ ...validSubmit, method: "gbp_oauth" }),
      unclaimed,
      ENABLED,
      NOW,
    );
    if (res.outcome !== "submitted") throw new Error("unreachable");
    expect(res.events.map((e) => e.type)).toEqual([
      "claim.submitted",
      "claim.verification_started",
    ]);
    expect(res.events[1]?.data).toEqual({
      method: "gbp_oauth",
      expires_at: null,
    });
  });

  it("throws when the stored tenant TTL is outside the §9.5 bounds", () => {
    expect(() =>
      handleClaimSubmit(submitCommand(validSubmit), unclaimed, ENABLED, NOW, {
        phone_otp_minutes: 120,
      }),
    ).toThrow(/bounds/);
  });

  it("emits only claim.submitted for the manual method", () => {
    const res = handleClaimSubmit(
      submitCommand({ ...validSubmit, method: "manual" }),
      unclaimed,
      ENABLED,
      NOW,
    );
    if (res.outcome !== "submitted") throw new Error("unreachable");
    expect(res.events.map((e) => e.type)).toEqual(["claim.submitted"]);
  });

  it("a claim on an already-claimed listing is a dispute, not a submission (§9.4)", () => {
    const res = handleClaimSubmit(submitCommand(validSubmit), claimed, ENABLED, NOW);

    expect(res.outcome).toBe("disputed");
    if (res.outcome !== "disputed") throw new Error("unreachable");

    // Never auto-transfer: the disputed claim is the whole result, no
    // claim.submitted and no listing.owner_assigned.
    expect(res.events).toHaveLength(1);
    expect(res.events.map((e) => e.type)).toEqual(["claim.disputed"]);
    const [disputed] = res.events;
    expect(disputed.subject).toBe("listing_hoffman");
    expect(disputed.data.claim.status).toBe("disputed");
    expect("id" in disputed.data.claim).toBe(false);
  });

  it("rejects when consent is absent (§9.0, invariant 7)", () => {
    const noConsent = {
      listing_id: "listing_hoffman",
      method: "phone_otp",
      claimant,
    };
    const res = handleClaimSubmit(submitCommand(noConsent), unclaimed, ENABLED, NOW);

    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(res.problem.status).toBe(422);
    expect(String(res.problem.detail)).toContain("consent");
  });

  it("rejects a consent entry missing at / ip / text_version", () => {
    const res = handleClaimSubmit(
      submitCommand({
        ...validSubmit,
        consent: { marketing_email: { granted: true } },
      }),
      unclaimed,
      ENABLED,
      NOW,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    const joined = JSON.stringify(res.problem.errors);
    expect(joined).toContain("marketing_email.at");
    expect(joined).toContain("marketing_email.ip");
    expect(joined).toContain("marketing_email.text_version");
  });

  it("rejects a granted consent entry whose at is null", () => {
    const res = handleClaimSubmit(
      submitCommand({
        ...validSubmit,
        consent: {
          marketing_email: {
            granted: true,
            at: null,
            ip: "203.0.113.44",
            text_version: "consent-v3",
          },
        },
      }),
      unclaimed,
      ENABLED,
      NOW,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(JSON.stringify(res.problem.errors)).toContain(
      "required when consent is granted",
    );
  });

  it("accepts a not-granted consent entry with null at / ip", () => {
    const res = handleClaimSubmit(
      submitCommand({
        ...validSubmit,
        method: "manual",
        consent: {
          automated_calls: {
            granted: false,
            at: null,
            ip: null,
            text_version: "consent-v3",
          },
        },
      }),
      unclaimed,
      ENABLED,
      NOW,
    );
    expect(res.outcome).toBe("submitted");
  });

  it("rejects a method the tenant has not enabled", () => {
    const res = handleClaimSubmit(
      submitCommand({ ...validSubmit, method: "postcard" }),
      unclaimed,
      ENABLED,
      NOW,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(JSON.stringify(res.problem.errors)).toContain("not enabled");
  });

  it("rejects an unknown method", () => {
    const res = handleClaimSubmit(
      submitCommand({ ...validSubmit, method: "carrier_pigeon" }),
      unclaimed,
      ENABLED,
      NOW,
    );
    expect(res.outcome).toBe("rejected");
  });

  it("rejects when the listing does not exist", () => {
    const res = handleClaimSubmit(submitCommand(validSubmit), null, ENABLED, NOW);
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(JSON.stringify(res.problem.errors)).toContain("does not exist");
  });

  it("rejects a claim against a suspended listing", () => {
    const res = handleClaimSubmit(
      submitCommand(validSubmit),
      { ...unclaimed, status: "suspended" },
      ENABLED,
      NOW,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(JSON.stringify(res.problem.errors)).toContain("suspended");
  });

  it("rejects a malformed envelope", () => {
    const res = handleClaimSubmit(
      submitCommand(validSubmit, { trace_id: "" }),
      unclaimed,
      ENABLED,
      NOW,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(res.problem.detail).toBe("malformed command envelope");
  });

  it("rejects the wrong command name", () => {
    const res = handleClaimSubmit(
      submitCommand(validSubmit, { command: "claim.approve" }),
      unclaimed,
      ENABLED,
      NOW,
    );
    expect(res.outcome).toBe("rejected");
  });
});

describe("withClaimId", () => {
  it("fills the claim id on the submitted draft and leaves verification_started alone", () => {
    const res = handleClaimSubmit(
      submitCommand(validSubmit),
      unclaimed,
      ENABLED,
      NOW,
    );
    if (res.outcome !== "submitted") throw new Error("unreachable");

    const events = withClaimId(res, "claim_minted_9");

    expect(events[0]).toEqual({
      type: "claim.submitted",
      subject: "listing_hoffman",
      data: {
        claim: {
          id: "claim_minted_9",
          listing_id: "listing_hoffman",
          status: "pending_verification",
          method: "phone_otp",
        },
        claimant: { ...claimant, email: "dana@hoffmanplumbing.example" },
        consent,
      },
    });
    expect(events[1]).toEqual({
      type: "claim.verification_started",
      subject: "listing_hoffman",
      data: { method: "phone_otp", expires_at: PHONE_OTP_DEADLINE },
    });
    // Draft not mutated.
    expect("id" in res.events[0].data.claim).toBe(false);
  });

  it("fills the claim id on a disputed draft", () => {
    const res = handleClaimSubmit(submitCommand(validSubmit), claimed, ENABLED, NOW);
    if (res.outcome !== "disputed") throw new Error("unreachable");

    const [disputed] = withClaimId(res, "claim_minted_10");
    expect(disputed?.type).toBe("claim.disputed");
    if (disputed?.type !== "claim.disputed") throw new Error("unreachable");
    expect(disputed.data.claim.id).toBe("claim_minted_10");
  });
});

// --- claim.approve -------------------------------------------------

describe("claim.approve", () => {
  it("emits claim.approved then listing.owner_assigned, in that order, same subject", () => {
    const res = handleClaimApprove(
      approveCommand({ claim_id: "claim_1", decided_by: "usr_admin" }),
      pendingClaim,
      unclaimed,
    );

    expect(res.outcome).toBe("approved");
    if (res.outcome !== "approved") throw new Error("unreachable");

    expect(res.events.map((e) => e.type)).toEqual([
      "claim.approved",
      "listing.owner_assigned",
    ]);
    const [approved, assigned] = res.events;
    expect(approved.subject).toBe("listing_hoffman");
    expect(assigned.subject).toBe("listing_hoffman");
    expect(approved.data).toEqual({
      claim: {
        id: "claim_1",
        listing_id: "listing_hoffman",
        method: "phone_otp",
      },
      decided_by: "usr_admin",
      manual_verification: null,
    });
    expect(assigned.data).toEqual({
      owner_user_id: "usr_dana",
      claim_id: "claim_1",
    });
  });

  it("never emits listing.claimed or claim.notified_existing_contacts", () => {
    const res = handleClaimApprove(
      approveCommand({ claim_id: "claim_1", decided_by: "usr_admin" }),
      pendingClaim,
      unclaimed,
    );
    if (res.outcome !== "approved") throw new Error("unreachable");
    const types = res.events.map((e) => e.type);
    expect(types).not.toContain("listing.claimed");
    expect(types).not.toContain("claim.notified_existing_contacts");
  });

  it("records manual_verification on the manual path", () => {
    const res = handleClaimApprove(
      approveCommand({
        claim_id: "claim_2",
        decided_by: "usr_admin",
        manual_verification: manualVerification,
      }),
      manualClaim,
      unclaimed,
    );
    if (res.outcome !== "approved") throw new Error("unreachable");
    expect(res.events[0].data.manual_verification).toEqual(manualVerification);
  });

  it("rejects a manual approval with no manual_verification (§9.3)", () => {
    const res = handleClaimApprove(
      approveCommand({ claim_id: "claim_2", decided_by: "usr_admin" }),
      manualClaim,
      unclaimed,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(res.problem.status).toBe(422);
    expect(JSON.stringify(res.problem.errors)).toContain("§9.3");
  });

  it("rejects a manual approval whose notes are empty", () => {
    const res = handleClaimApprove(
      approveCommand({
        claim_id: "claim_2",
        decided_by: "usr_admin",
        manual_verification: { ...manualVerification, notes: "   " },
      }),
      manualClaim,
      unclaimed,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(JSON.stringify(res.problem.errors)).toContain(
      "manual_verification.notes",
    );
  });

  it("rejects a manual approval whose notes key is absent", () => {
    const noNotes = {
      method_used: "phone",
      verified_by: "usr_admin",
      verified_at: "2026-08-28T16:04:00.000Z",
      evidence_ref: null,
    };
    const res = handleClaimApprove(
      approveCommand({
        claim_id: "claim_2",
        decided_by: "usr_admin",
        manual_verification: noNotes,
      }),
      manualClaim,
      unclaimed,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(JSON.stringify(res.problem.errors)).toContain(
      "manual_verification.notes",
    );
  });

  it("rejects manual_verification supplied for a non-manual claim", () => {
    const res = handleClaimApprove(
      approveCommand({
        claim_id: "claim_1",
        decided_by: "usr_admin",
        manual_verification: manualVerification,
      }),
      pendingClaim,
      unclaimed,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(JSON.stringify(res.problem.errors)).toContain("only valid when");
  });

  it("rejects when the claim does not exist", () => {
    const res = handleClaimApprove(
      approveCommand({ claim_id: "claim_1", decided_by: "usr_admin" }),
      null,
      unclaimed,
    );
    expect(res.outcome).toBe("rejected");
  });

  it("rejects a claim that is not pending or verifying", () => {
    const res = handleClaimApprove(
      approveCommand({ claim_id: "claim_1", decided_by: "usr_admin" }),
      { ...pendingClaim, status: "approved" },
      unclaimed,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(JSON.stringify(res.problem.errors)).toContain("cannot be approved");
  });

  it("rejects a claim with no claimant user to assign", () => {
    const res = handleClaimApprove(
      approveCommand({ claim_id: "claim_1", decided_by: "usr_admin" }),
      { ...pendingClaim, claimant_user_id: null },
      unclaimed,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(JSON.stringify(res.problem.errors)).toContain("no claimant user");
  });

  it("rejects when payload.claim_id does not match the claim record", () => {
    const res = handleClaimApprove(
      approveCommand({ claim_id: "claim_999", decided_by: "usr_admin" }),
      pendingClaim,
      unclaimed,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(JSON.stringify(res.problem.errors)).toContain("does not match");
  });

  it("rejects a malformed envelope", () => {
    const res = handleClaimApprove(
      approveCommand(
        { claim_id: "claim_1", decided_by: "usr_admin" },
        {
          tenant_id: "",
        },
      ),
      pendingClaim,
      unclaimed,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(res.problem.detail).toBe("malformed command envelope");
  });
});
