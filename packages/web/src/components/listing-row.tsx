import { resolvePublicRender } from "@osds/core";
import type { EntitlementStatus } from "@osds/core";

export interface ListingRowProps {
  /** Listing detail link, or null to render the name unlinked. */
  readonly href: string | null;
  readonly name: string;
  readonly entitlementStatus: EntitlementStatus;
  /** Tier name; shown only when the §6.5 resolver says the badge is visible. */
  readonly tier: string | null;
  /** Category names, shown after the badge. Omit to hide. */
  readonly categories?: readonly string[];
  /** Locality, shown last. Omit or pass null to hide. */
  readonly locality?: string | null;
}

/**
 * One listing in a list (category page, search results). The tier badge is
 * decided by the §6.5 resolver here, so every list renders it identically.
 */
export function ListingRow(props: ListingRowProps) {
  const { badge } = resolvePublicRender(props.entitlementStatus);

  return (
    <li>
      {props.href !== null ? <a href={props.href}>{props.name}</a> : props.name}
      {badge === "tier" && props.tier !== null ? <span> {props.tier}</span> : null}
      {props.categories !== undefined && props.categories.length > 0 ? (
        <span> {props.categories.join(", ")}</span>
      ) : null}
      {props.locality !== undefined && props.locality !== null ? (
        <span> {props.locality}</span>
      ) : null}
    </li>
  );
}
