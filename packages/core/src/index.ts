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
  type EmittedClaimEvent,
} from "./command/index.js";
