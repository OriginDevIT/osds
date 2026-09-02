/**
 * Staff roles - spec §4.4 ("Role"). Five ordered ranks, each a superset of the
 * one below, so authorization is a comparison (`rank >= n`) rather than a
 * capability matrix. The load-bearing cuts are 3/2 (money and PII) and 2/1
 * (authority over other people's listings).
 *
 * This is a spec rule with no driver and no I/O, so it lives on the `@osds/core`
 * root entrypoint. `@osds/api` re-exports it for request-context callers.
 *
 * `is_superadmin` is NOT a role - it is installation scope on `operators`, a
 * separate axis (spec §4.4, "Two axes, not one ladder"). It never appears here.
 */

/** A staff membership's permission level within one tenant. `staff_memberships.role`. */
export type StaffRole = "admin" | "manager" | "editor" | "moderator" | "support";

/**
 * Ordinal for each {@link StaffRole}. Higher outranks lower; compare with
 * `ROLE_RANK[a] >= ROLE_RANK[b]`. The values are the spec §4.4 table verbatim
 * and are a CHECK-constraint-style contract: adding a rank is additive, but
 * re-meaning an existing one is a data migration (spec §4.4, "Adding a role
 * later").
 */
export const ROLE_RANK: Readonly<Record<StaffRole, number>> = {
  admin: 4,
  manager: 3,
  editor: 2,
  moderator: 1,
  support: 0,
};
