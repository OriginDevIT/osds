/**
 * `@osds/core/persist` - the command persistence layer.
 *
 * Everything reachable from here opens a Postgres transaction and therefore
 * pulls in kysely and pg. Keep it out of the package root: `packages/web`
 * imports the pure resolvers from `@osds/core` and a database driver must not
 * end up in the Next bundle (issue #26).
 *
 * Covers `listing.upsert` and the claim commands (`claim.submit`,
 * `claim.approve`). The entitlement command handler is re-exported here rather
 * than from the root for the same bundle reason.
 */
export {
  persistListingUpsert,
  type PersistDeps,
  type PersistListingUpsertResult,
} from "./listing-upsert.js";

export {
  persistClaimSubmit,
  persistClaimApprove,
  type PersistClaimSubmitResult,
  type PersistClaimApproveResult,
} from "./claim.js";

export { handleCommand } from "../command/handle.js";
export type { CommandContext, IdFactory } from "../command/context.js";
