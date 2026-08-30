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
} from "./entitlement";

export {
  resolvePublicRender,
  type PublicRender,
  type BadgeVisibility,
  type PerkLevel,
  type OwnerNotice,
} from "./public-render";
