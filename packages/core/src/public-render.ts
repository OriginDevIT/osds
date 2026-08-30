/**
 * §6.5 - what the public page and owner dashboard show for a given entitlement
 * status. Pure lookup.
 *
 * The bug this prevents: a listing rendering as Featured while the card has been
 * declining for weeks. `past_due` therefore keeps the badge, the featured slot
 * and full perks - most failed payments are involuntary - while `grace` and
 * `expired` drop to rank-0.
 */
import type { EntitlementStatus } from "./entitlement.js";

/** Whether the tier badge shows. */
export type BadgeVisibility = "tier" | "none";

/** Which perk set the page renders (gallery, links, description, ...). */
export type PerkLevel = "full" | "rank_0";

/** The owner-facing message. Concrete copy (day counts, dates) is filled in by the caller. */
export type OwnerNotice =
  | "normal"
  | "trial_ending"
  | "payment_failed_banner"
  | "downgraded_banner"
  | "upgrade_prompt"
  | "canceled_until_date";

export interface PublicRender {
  readonly badge: BadgeVisibility;
  readonly featuredPlacement: boolean;
  readonly perks: PerkLevel;
  readonly ownerNotice: OwnerNotice;
}

/** Resolve the §6.5 row for a status. `none` follows §6.2 - rank-0, nothing to show. */
export function resolvePublicRender(status: EntitlementStatus): PublicRender {
  switch (status) {
    case "trialing":
      return { badge: "tier", featuredPlacement: true, perks: "full", ownerNotice: "trial_ending" };
    case "active":
      return { badge: "tier", featuredPlacement: true, perks: "full", ownerNotice: "normal" };
    case "past_due":
      return {
        badge: "tier",
        featuredPlacement: true,
        perks: "full",
        ownerNotice: "payment_failed_banner",
      };
    case "grace":
      return {
        badge: "none",
        featuredPlacement: false,
        perks: "rank_0",
        ownerNotice: "downgraded_banner",
      };
    case "expired":
      return {
        badge: "none",
        featuredPlacement: false,
        perks: "rank_0",
        ownerNotice: "upgrade_prompt",
      };
    case "canceled":
      return {
        badge: "tier",
        featuredPlacement: true,
        perks: "full",
        ownerNotice: "canceled_until_date",
      };
    case "comped":
      // §6.8: a normal tier badge, nothing indicating comp status.
      return { badge: "tier", featuredPlacement: true, perks: "full", ownerNotice: "normal" };
    case "none":
      return { badge: "none", featuredPlacement: false, perks: "rank_0", ownerNotice: "normal" };
  }
}
