/**
 * @osds/core - domain logic. Pure, no I/O.
 *
 * The DB-backed command layer (handleCommand, persistListingUpsert, ...) is
 * exported from "@osds/core/persist", not here: this entrypoint must not pull
 * kysely or pg into a consumer's bundle (issue #26).
 */
export {
  transition,
  IllegalTransitionError,
  type EntitlementStatus,
  type EntitlementTrigger,
  type EntitlementTriggerType,
  type EmittedEventType,
  type TransitionResult,
} from "./entitlement.js";

export {
  resolvePublicRender,
  type PublicRender,
  type BadgeVisibility,
  type PerkLevel,
  type OwnerNotice,
} from "./public-render.js";

export {
  hash as hashPassword,
  verify as verifyPassword,
  needsRehash as passwordNeedsRehash,
  InvalidPasswordHashError,
} from "./password.js";

export { ROLE_RANK, type StaffRole } from "./roles.js";

export { newUlid, encodeUlid, ulidFactory } from "./ulid.js";
export type { IdFactory } from "./command/context.js";

export {
  CommandRejected,
  validationProblem,
  scopeProblem,
  parseCommand,
  type ParsedCommand,
  type PaymentOutcome,
  handleListingUpsert,
  withSubject,
  jsonPatch,
  deepEqual,
  type JsonPatchOp,
  type ListingUpsertResult,
  type ListingMatch,
  type ListingCreatedEvent,
  type ListingCreatedDraft,
  type ListingUpdatedEvent,
  type Listing,
  type ListingContent,
  type CreatedListing,
  type ListingLocation,
  type ListingContact,
  type GeoPrecision,
  handleClaimSubmit,
  handleClaimApprove,
  withClaimId,
  type ClaimSubmitResult,
  type ClaimApproveResult,
  type ClaimMethod,
  type ClaimStatus,
  type ManualMethodUsed,
  type ClaimListing,
  type ClaimRecord,
  type ClaimantData,
  type ConsentEntry,
  type ConsentMap,
  type ManualVerification,
  type ClaimSubmittedEvent,
  type ClaimSubmittedDraft,
  type ClaimDisputedEvent,
  type ClaimDisputedDraft,
  type ClaimVerificationStartedEvent,
  type ClaimApprovedEvent,
  type ListingOwnerAssignedEvent,
  type UserCreatedEvent,
  type EmittedClaimEvent,
  resolveVerificationTtl,
  type VerificationTtlConfig,
} from "./command/index.js";
