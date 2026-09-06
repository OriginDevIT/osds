"""The canonical registry of every OSDS event type.

Event names are facts, past tense, immutable (spec §1, §3.3). A rename is a new
type plus deprecation of the old one, never an edit. Every type the service
layer emits MUST be one of the constants below; ``ALL_EVENT_TYPES`` is the flat
set, and ``audit/tests/test_event_types.py`` keeps the two in lockstep and
checks the catalogue against the spec.

This module replaces the compile-time union the archived TypeScript build had
(decisions.md §4).

Deferred namespaces -- ``media.*`` and ``search.*`` (spec §3.4) -- are
deliberately absent. Adding them later is additive and non-breaking.
"""

from __future__ import annotations

# --- listing.* -----------------------------------------------------------------
LISTING_CREATED = "listing.created"
LISTING_UPDATED = "listing.updated"
LISTING_PUBLISHED = "listing.published"
LISTING_UNPUBLISHED = "listing.unpublished"
LISTING_MERGED = "listing.merged"
LISTING_DELETED = "listing.deleted"
LISTING_OWNER_ASSIGNED = "listing.owner_assigned"
LISTING_TIER_CHANGED = "listing.tier_changed"
LISTING_EXPIRING_SOON = "listing.expiring_soon"
LISTING_EXPIRED = "listing.expired"

# --- claim.* -----------------------------------------------------------------
CLAIM_SUBMITTED = "claim.submitted"
CLAIM_VERIFICATION_STARTED = "claim.verification_started"
CLAIM_VERIFICATION_FAILED = "claim.verification_failed"
CLAIM_APPROVED = "claim.approved"
CLAIM_REJECTED = "claim.rejected"
CLAIM_ABANDONED = "claim.abandoned"
CLAIM_NOTIFIED_EXISTING_CONTACTS = "claim.notified_existing_contacts"
CLAIM_DISPUTED = "claim.disputed"

# --- user.* ----------------------------------------------------------------
USER_CREATED = "user.created"

# --- staff.* ---------------------------------------------------------------
STAFF_INVITED = "staff.invited"
STAFF_ACCEPTED = "staff.accepted"
STAFF_ROLE_CHANGED = "staff.role_changed"
STAFF_REMOVED = "staff.removed"

# --- billing.* -----------------------------------------------------------------
BILLING_CHECKOUT_STARTED = "billing.checkout_started"
BILLING_SUBSCRIPTION_STARTED = "billing.subscription_started"
BILLING_SUBSCRIPTION_CHANGED = "billing.subscription_changed"
BILLING_PAYMENT_SUCCEEDED = "billing.payment_succeeded"
BILLING_PAYMENT_FAILED = "billing.payment_failed"
BILLING_SUBSCRIPTION_CANCELED = "billing.subscription_canceled"
BILLING_REFUND_ISSUED = "billing.refund_issued"

# --- entitlement.* -----------------------------------------------------------
ENTITLEMENT_STARTED = "entitlement.started"
ENTITLEMENT_TRIAL_CONVERTED = "entitlement.trial_converted"
ENTITLEMENT_DUNNING_STARTED = "entitlement.dunning_started"
ENTITLEMENT_RECOVERED = "entitlement.recovered"
ENTITLEMENT_DOWNGRADED = "entitlement.downgraded"
ENTITLEMENT_RESTORED = "entitlement.restored"
ENTITLEMENT_RENEWAL_DUE = "entitlement.renewal_due"
ENTITLEMENT_EXPIRED = "entitlement.expired"
ENTITLEMENT_CANCELED = "entitlement.canceled"
ENTITLEMENT_OVERRIDDEN = "entitlement.overridden"

# --- slot.* ----------------------------------------------------------------
SLOT_HELD = "slot.held"
SLOT_HOLD_RELEASED = "slot.hold_released"
SLOT_OCCUPIED = "slot.occupied"
SLOT_RELEASED = "slot.released"
SLOT_WAITLIST_JOINED = "slot.waitlist_joined"
SLOT_WAITLIST_NOTIFIED = "slot.waitlist_notified"
SLOT_WAITLIST_CLEARED = "slot.waitlist_cleared"
SLOT_CAPACITY_CHANGED = "slot.capacity_changed"

# --- lead.* / call.* -------------------------------------------------------
LEAD_CAPTURED = "lead.captured"
LEAD_DELIVERED = "lead.delivered"
LEAD_DELIVERY_FAILED = "lead.delivery_failed"
LEAD_MARKED_SPAM = "lead.marked_spam"
CALL_TRACKED = "call.tracked"

# --- review.* --------------------------------------------------------------
REVIEW_SUBMITTED = "review.submitted"
REVIEW_PUBLISHED = "review.published"
REVIEW_FLAGGED = "review.flagged"
REVIEW_REMOVED = "review.removed"
REVIEW_RESPONDED = "review.responded"

# --- moderation.* --------------------------------------------------------------
MODERATION_QUEUED = "moderation.queued"
MODERATION_DECIDED = "moderation.decided"

# --- compliance.* ------------------------------------------------------------
COMPLIANCE_REMOVAL_REQUESTED = "compliance.removal_requested"
COMPLIANCE_REMOVAL_COMPLETED = "compliance.removal_completed"
COMPLIANCE_DATA_EXPORTED = "compliance.data_exported"
COMPLIANCE_CONSENT_CHANGED = "compliance.consent_changed"

# --- agent.* ---------------------------------------------------------------
AGENT_ACTION_TAKEN = "agent.action_taken"
AGENT_ESCALATION_REQUESTED = "agent.escalation_requested"
AGENT_ESCALATION_RESOLVED = "agent.escalation_resolved"
AGENT_BLOCKED = "agent.blocked"

# --- tenant.* -- the only namespace that is not tenant-scoped ---------------
TENANT_CREATED = "tenant.created"
TENANT_DOMAIN_VERIFIED = "tenant.domain_verified"
TENANT_SETTINGS_CHANGED = "tenant.settings_changed"
TENANT_SUSPENDED = "tenant.suspended"

# --- import.* ------------------------------------------------------------------
IMPORT_STARTED = "import.started"
IMPORT_COMPLETED = "import.completed"
IMPORT_ROLLED_BACK = "import.rolled_back"

# --- postal.* ------------------------------------------------------------------
POSTAL_DISPATCHED = "postal.dispatched"
POSTAL_FAILED = "postal.failed"


ALL_EVENT_TYPES: "frozenset[str]" = frozenset(
    {
        LISTING_CREATED,
        LISTING_UPDATED,
        LISTING_PUBLISHED,
        LISTING_UNPUBLISHED,
        LISTING_MERGED,
        LISTING_DELETED,
        LISTING_OWNER_ASSIGNED,
        LISTING_TIER_CHANGED,
        LISTING_EXPIRING_SOON,
        LISTING_EXPIRED,
        CLAIM_SUBMITTED,
        CLAIM_VERIFICATION_STARTED,
        CLAIM_VERIFICATION_FAILED,
        CLAIM_APPROVED,
        CLAIM_REJECTED,
        CLAIM_ABANDONED,
        CLAIM_NOTIFIED_EXISTING_CONTACTS,
        CLAIM_DISPUTED,
        USER_CREATED,
        STAFF_INVITED,
        STAFF_ACCEPTED,
        STAFF_ROLE_CHANGED,
        STAFF_REMOVED,
        BILLING_CHECKOUT_STARTED,
        BILLING_SUBSCRIPTION_STARTED,
        BILLING_SUBSCRIPTION_CHANGED,
        BILLING_PAYMENT_SUCCEEDED,
        BILLING_PAYMENT_FAILED,
        BILLING_SUBSCRIPTION_CANCELED,
        BILLING_REFUND_ISSUED,
        ENTITLEMENT_STARTED,
        ENTITLEMENT_TRIAL_CONVERTED,
        ENTITLEMENT_DUNNING_STARTED,
        ENTITLEMENT_RECOVERED,
        ENTITLEMENT_DOWNGRADED,
        ENTITLEMENT_RESTORED,
        ENTITLEMENT_RENEWAL_DUE,
        ENTITLEMENT_EXPIRED,
        ENTITLEMENT_CANCELED,
        ENTITLEMENT_OVERRIDDEN,
        SLOT_HELD,
        SLOT_HOLD_RELEASED,
        SLOT_OCCUPIED,
        SLOT_RELEASED,
        SLOT_WAITLIST_JOINED,
        SLOT_WAITLIST_NOTIFIED,
        SLOT_WAITLIST_CLEARED,
        SLOT_CAPACITY_CHANGED,
        LEAD_CAPTURED,
        LEAD_DELIVERED,
        LEAD_DELIVERY_FAILED,
        LEAD_MARKED_SPAM,
        CALL_TRACKED,
        REVIEW_SUBMITTED,
        REVIEW_PUBLISHED,
        REVIEW_FLAGGED,
        REVIEW_REMOVED,
        REVIEW_RESPONDED,
        MODERATION_QUEUED,
        MODERATION_DECIDED,
        COMPLIANCE_REMOVAL_REQUESTED,
        COMPLIANCE_REMOVAL_COMPLETED,
        COMPLIANCE_DATA_EXPORTED,
        COMPLIANCE_CONSENT_CHANGED,
        AGENT_ACTION_TAKEN,
        AGENT_ESCALATION_REQUESTED,
        AGENT_ESCALATION_RESOLVED,
        AGENT_BLOCKED,
        TENANT_CREATED,
        TENANT_DOMAIN_VERIFIED,
        TENANT_SETTINGS_CHANGED,
        TENANT_SUSPENDED,
        IMPORT_STARTED,
        IMPORT_COMPLETED,
        IMPORT_ROLLED_BACK,
        POSTAL_DISPATCHED,
        POSTAL_FAILED,
    }
)

# Namespaces defined here. ``tenant`` is the only one whose events carry no
# tenant block on the envelope (spec §3.2).
NAMESPACES: "frozenset[str]" = frozenset(t.split(".", 1)[0] for t in ALL_EVENT_TYPES)

# Specified but not yet in the registry (spec §3.4).
DEFERRED_NAMESPACES: "frozenset[str]" = frozenset({"media", "search"})


def is_known_event_type(event_type: str) -> bool:
    """True if ``event_type`` is a registered OSDS event type."""
    return event_type in ALL_EVENT_TYPES
