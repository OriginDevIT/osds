// The DB-touching handlers (handleCommand, persist*) and their CommandContext
// live under "@osds/core/persist" so the root entrypoint stays free of kysely
// and pg - see issue #26 and src/persist/index.ts.
export { CommandRejected, validationProblem, scopeProblem } from "./problem.js";
export {
  parseCommand,
  type ParsedCommand,
  type PaymentOutcome,
} from "./validate.js";
export {
  handleListingUpsert,
  withSubject,
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
} from "./listing-upsert.js";
export { jsonPatch, deepEqual, type JsonPatchOp } from "./json-patch.js";
export {
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
} from "./claim.js";
export {
  resolveVerificationTtl,
  type VerificationTtlConfig,
} from "./verification-ttl.js";
