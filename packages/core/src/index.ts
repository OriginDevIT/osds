/**
 * @osds/core - domain logic. Pure, no I/O.
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
  handleCommand,
  CommandRejected,
  validationProblem,
  scopeProblem,
  parseCommand,
  type ParsedCommand,
  type PaymentOutcome,
  type CommandContext,
  type IdFactory,
} from "./command/index.js";
