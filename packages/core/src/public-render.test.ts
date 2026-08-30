import { describe, it, expect } from "vitest";
import { resolvePublicRender } from "./public-render.js";

/** One assertion per row of the §6.5 table. */
describe("§6.5 public page rendering by state", () => {
  it("trialing: tier badge, featured, full perks, trial-ending notice", () => {
    expect(resolvePublicRender("trialing")).toEqual({
      badge: "tier",
      featuredPlacement: true,
      perks: "full",
      ownerNotice: "trial_ending",
    });
  });

  it("active: tier badge, featured, full perks, normal", () => {
    expect(resolvePublicRender("active")).toEqual({
      badge: "tier",
      featuredPlacement: true,
      perks: "full",
      ownerNotice: "normal",
    });
  });

  it("past_due: badge, featured and full perks all retained; payment-failed banner", () => {
    expect(resolvePublicRender("past_due")).toEqual({
      badge: "tier",
      featuredPlacement: true,
      perks: "full",
      ownerNotice: "payment_failed_banner",
    });
  });

  it("grace: no badge, no featured, rank-0 perks, downgraded banner", () => {
    expect(resolvePublicRender("grace")).toEqual({
      badge: "none",
      featuredPlacement: false,
      perks: "rank_0",
      ownerNotice: "downgraded_banner",
    });
  });

  it("expired: no badge, no featured, rank-0 perks, upgrade prompt", () => {
    expect(resolvePublicRender("expired")).toEqual({
      badge: "none",
      featuredPlacement: false,
      perks: "rank_0",
      ownerNotice: "upgrade_prompt",
    });
  });

  it("canceled (pre-period-end): badge, featured, full perks, active-until-date notice", () => {
    expect(resolvePublicRender("canceled")).toEqual({
      badge: "tier",
      featuredPlacement: true,
      perks: "full",
      ownerNotice: "canceled_until_date",
    });
  });

  it("comped: normal tier badge, featured, full perks, nothing indicating comp", () => {
    expect(resolvePublicRender("comped")).toEqual({
      badge: "tier",
      featuredPlacement: true,
      perks: "full",
      ownerNotice: "normal",
    });
  });
});

describe("§6.2 none", () => {
  it("renders as rank-0 with nothing to show", () => {
    expect(resolvePublicRender("none")).toEqual({
      badge: "none",
      featuredPlacement: false,
      perks: "rank_0",
      ownerNotice: "normal",
    });
  });
});
