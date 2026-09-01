/**
 * Verification code lifetime - spec §9.5.
 *
 * A lifetime is a rule, and rules belong to core: `claim.submit` computes
 * `expires_at`, the caller never supplies it. A tenant may tune the lifetime
 * within bounds core enforces; it may not configure one that makes the method
 * meaningless.
 *
 * Pure, like the rest of `./claim.ts`: no clock, no I/O. The caller passes the
 * tenant's `claim_verification.ttl` config (threaded like `enabledMethods` is)
 * and this returns a duration; `handleClaimSubmit` adds it to the injected
 * clock. Reading the config off a tenant settings table is a separate concern -
 * there is no home for it in the schema yet (issue filed).
 *
 * | method        | default  | minimum | maximum |
 * |---------------|----------|---------|---------|
 * | phone_otp     | 10 min   | 5 min   | 60 min  |
 * | domain_email  | 24 h     | 15 min  | 48 h    |
 * | postcard      | 21 days  | 7 days  | 45 days |
 * | gbp_oauth     | -        | -       | -       |
 * | manual        | -        | -       | -       |
 *
 * `gbp_oauth` has no OSDS-side code (Google owns that session) and `manual` has
 * no code at all, so neither has a TTL - {@link resolveVerificationTtl} returns
 * `null` for them and `claim.verification_started.expires_at` is `null`.
 */
import type { ClaimMethod } from "./claim.js";

/**
 * The `ttl` sub-object of the §9.5 `claim_verification` tenant config. Every
 * key is optional; an absent key means "use the default". `enabled_methods` is
 * the sibling key and is threaded separately as `enabledMethods`.
 */
export interface VerificationTtlConfig {
  readonly phone_otp_minutes?: number;
  readonly domain_email_minutes?: number;
  readonly postcard_days?: number;
}

const MINUTE_MS = 60_000;
const DAY_MS = 86_400_000;

interface TtlBound {
  /** Which {@link VerificationTtlConfig} key overrides the default. */
  readonly configKey: keyof VerificationTtlConfig;
  /** Milliseconds per config unit (minutes or days). */
  readonly unitMs: number;
  readonly defaultUnits: number;
  readonly minUnits: number;
  readonly maxUnits: number;
}

/** §9.5 bounds. Methods absent from this map have no OSDS-side code. */
const BOUNDS: Partial<Record<ClaimMethod, TtlBound>> = {
  phone_otp: {
    configKey: "phone_otp_minutes",
    unitMs: MINUTE_MS,
    defaultUnits: 10,
    minUnits: 5,
    maxUnits: 60,
  },
  domain_email: {
    configKey: "domain_email_minutes",
    unitMs: MINUTE_MS,
    defaultUnits: 24 * 60,
    minUnits: 15,
    maxUnits: 48 * 60,
  },
  postcard: {
    configKey: "postcard_days",
    unitMs: DAY_MS,
    defaultUnits: 21,
    minUnits: 7,
    maxUnits: 45,
  },
};

/**
 * Milliseconds a freshly issued verification code stays valid, or `null` for a
 * method with no OSDS-side code (`manual`, `gbp_oauth`).
 *
 * With no config, or no key for this method, the §9.5 default applies. A
 * configured value must be a finite number within the method's `[min, max]`
 * bounds; anything else **throws** - a stored TTL outside the bounds is rejected
 * at use, never silently clamped (§9.5), and a malformed value never falls back
 * to the default and masks the misconfiguration.
 */
export function resolveVerificationTtl(
  method: ClaimMethod,
  config?: VerificationTtlConfig,
): number | null {
  const bound = BOUNDS[method];
  if (bound === undefined) return null;

  const configured = config?.[bound.configKey];
  if (configured === undefined) return bound.defaultUnits * bound.unitMs;

  if (
    typeof configured !== "number" ||
    !Number.isFinite(configured) ||
    configured < bound.minUnits ||
    configured > bound.maxUnits
  ) {
    throw new RangeError(
      `claim_verification.ttl.${bound.configKey} = ${JSON.stringify(configured)} ` +
        `is not within the §9.5 bounds [${bound.minUnits}, ${bound.maxUnits}] ` +
        `for method "${method}"`,
    );
  }

  return configured * bound.unitMs;
}
