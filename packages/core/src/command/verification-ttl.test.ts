import { describe, it, expect } from "vitest";
import { resolveVerificationTtl } from "./verification-ttl.js";

const MIN = 60_000;
const DAY = 86_400_000;

describe("resolveVerificationTtl", () => {
  it("returns the §9.5 default when no config is given", () => {
    expect(resolveVerificationTtl("phone_otp")).toBe(10 * MIN);
    expect(resolveVerificationTtl("domain_email")).toBe(24 * 60 * MIN);
    expect(resolveVerificationTtl("postcard")).toBe(21 * DAY);
  });

  it("returns the default when the config omits this method's key", () => {
    expect(resolveVerificationTtl("phone_otp", { postcard_days: 30 })).toBe(
      10 * MIN,
    );
  });

  it("uses an in-bounds configured value", () => {
    expect(resolveVerificationTtl("phone_otp", { phone_otp_minutes: 5 })).toBe(
      5 * MIN,
    );
    expect(resolveVerificationTtl("phone_otp", { phone_otp_minutes: 60 })).toBe(
      60 * MIN,
    );
    expect(
      resolveVerificationTtl("domain_email", { domain_email_minutes: 15 }),
    ).toBe(15 * MIN);
    expect(
      resolveVerificationTtl("domain_email", { domain_email_minutes: 2880 }),
    ).toBe(2880 * MIN);
    expect(resolveVerificationTtl("postcard", { postcard_days: 7 })).toBe(
      7 * DAY,
    );
    expect(resolveVerificationTtl("postcard", { postcard_days: 45 })).toBe(
      45 * DAY,
    );
  });

  it("throws for a value below the minimum - no clamping", () => {
    expect(() =>
      resolveVerificationTtl("phone_otp", { phone_otp_minutes: 4 }),
    ).toThrow(/bounds \[5, 60\]/);
    expect(() =>
      resolveVerificationTtl("domain_email", { domain_email_minutes: 14 }),
    ).toThrow(/bounds \[15, 2880\]/);
    expect(() =>
      resolveVerificationTtl("postcard", { postcard_days: 6 }),
    ).toThrow(/bounds \[7, 45\]/);
  });

  it("throws for a value above the maximum - no clamping", () => {
    expect(() =>
      resolveVerificationTtl("phone_otp", { phone_otp_minutes: 61 }),
    ).toThrow(/bounds \[5, 60\]/);
    expect(() =>
      resolveVerificationTtl("domain_email", { domain_email_minutes: 2881 }),
    ).toThrow(/bounds/);
    expect(() =>
      resolveVerificationTtl("postcard", { postcard_days: 46 }),
    ).toThrow(/bounds/);
  });

  it("throws for a non-finite / non-number configured value", () => {
    expect(() =>
      resolveVerificationTtl("phone_otp", {
        phone_otp_minutes: Number.NaN,
      }),
    ).toThrow(/bounds/);
    expect(() =>
      resolveVerificationTtl("phone_otp", {
        // exercised path: a JSON config that stored a string
        phone_otp_minutes: "10" as unknown as number,
      }),
    ).toThrow(/bounds/);
  });

  it("returns null for methods with no OSDS-side code", () => {
    expect(resolveVerificationTtl("manual")).toBeNull();
    expect(resolveVerificationTtl("gbp_oauth")).toBeNull();
    // config for such a method is inert, never consulted
    expect(
      resolveVerificationTtl("gbp_oauth", { phone_otp_minutes: 999 }),
    ).toBeNull();
  });
});
