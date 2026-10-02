"""The operator's entitlement commands: comp, revoke, override
(spec §6.8, §7; decisions.md §4.11).

``entitlement.grant``, ``entitlement.revoke`` and ``entitlement.override`` are
manager actions (spec §4.4: "comps, entitlement overrides"). Each follows the
orchestrator shape: ``require_autocommit``, ``log_received``, an atomic apply,
``log_conclude``. A refusal changes nothing and concludes ``rejected``; an
operator below the role concludes ``blocked``. The payload carries ids, never
the operator's free-text reason.

Every one is an admin override and emits ``entitlement.overridden`` with the
operator and the reason (spec §6.8); ``apply_trigger`` does the rest.

Call with the tenant in ambient scope.
"""

from __future__ import annotations

from django.utils import timezone

from audit.command_log import log_conclude, log_received, require_autocommit
from billing import entitlements, machine
from billing.models import Entitlement, Tier
from directory.models import Listing
from tenants.models import StaffMembership

MANAGE_ROLE = StaffMembership.Role.MANAGER


class EntitlementRefused(Exception):
    """A guard refusal: nothing written. ``reason`` is a stable code."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


REFUSAL_TEXT = {
    "forbidden": "Your role does not allow this action.",
    "reason_required": "A reason is required.",
    "tier_unknown": "Choose a tier.",
    "tier_slot": "Slot-backed tiers are not available yet.",
    "listing_unknown": "No such listing.",
    "expiry_in_past": "The expiry must be in the future.",
    "not_comped": "Only a comped listing can be revoked.",
    "invalid_state": "That change is not allowed from the listing's current state.",
    "bad_parameters": "That change is missing something it needs.",
}


def refusal_text(reason: str) -> str:
    return REFUSAL_TEXT.get(reason, "This action was refused.")


def _membership(tenant, operator):
    return StaffMembership.objects.filter(
        operator=operator, tenant=tenant, status=StaffMembership.Status.ACTIVE
    ).first()


def _actor(operator, membership) -> dict:
    kind = "admin" if membership and membership.role >= StaffMembership.Role.ADMIN else "staff"
    return {"type": kind, "id": operator.public_id}


def _run(tenant, *, command, operator, listing, payload, apply):
    require_autocommit()
    membership = _membership(tenant, operator)
    actor = _actor(operator, membership)
    row = log_received(
        command=command, tenant=tenant, idempotency_key=None, actor=actor,
        trace_id=None, origin="", payload={"listing_id": listing.public_id, **payload},
    )
    try:
        if membership is None or membership.role < MANAGE_ROLE:
            raise EntitlementRefused("forbidden")
        outcome = apply(actor)
    except EntitlementRefused as exc:
        log_conclude(
            row, outcome="blocked" if exc.reason == "forbidden" else "rejected",
            problem={"reason": exc.reason},
        )
        raise
    except (machine.InvalidTransition, entitlements.EntitlementError) as exc:
        reason = "invalid_state" if isinstance(exc, machine.InvalidTransition) else "bad_parameters"
        log_conclude(row, outcome="rejected", problem={"reason": reason})
        raise EntitlementRefused(reason) from exc
    log_conclude(row, outcome="applied")
    return outcome


def _tier(tenant, tier_key) -> Tier:
    tier = Tier.objects.filter(tenant=tenant, key=tier_key).first() if tier_key else None
    if tier is None:
        raise EntitlementRefused("tier_unknown")
    if tier.uses_slot:
        raise EntitlementRefused("tier_slot")
    return tier


def _reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise EntitlementRefused("reason_required")
    return reason


def grant_comp(tenant, *, listing: Listing, operator, tier_key: str, reason: str,
               expires_at=None, now=None):
    """The ``entitlement.grant`` command: a tier with no payment, optionally
    until ``expires_at`` (spec §6.8). The public page shows an ordinary badge;
    nothing indicates a comp."""
    now = now or timezone.now()

    def apply(actor):
        why = _reason(reason)
        tier = _tier(tenant, tier_key)
        if expires_at is not None and expires_at <= now:
            raise EntitlementRefused("expiry_in_past")
        return entitlements.apply_trigger(
            tenant, listing, machine.Trigger.GRANT, now=now, actor=actor,
            tier=tier, operator=operator, reason=why, expires_at=expires_at,
        )

    return _run(tenant, command="entitlement.grant", operator=operator, listing=listing,
                payload={"tier": tier_key, "expires": expires_at is not None}, apply=apply)


def revoke_comp(tenant, *, listing: Listing, operator, reason: str, now=None):
    """The ``entitlement.revoke`` command: end a comp now (G9)."""
    now = now or timezone.now()

    def apply(actor):
        why = _reason(reason)
        ent = Entitlement.objects.filter(listing=listing).first()
        if ent is None or ent.status != Entitlement.Status.COMPED:
            raise EntitlementRefused("not_comped")
        return entitlements.apply_trigger(
            tenant, listing, machine.Trigger.REVOKE, now=now, actor=actor,
            operator=operator, reason=why,
        )

    return _run(tenant, command="entitlement.revoke", operator=operator, listing=listing,
                payload={}, apply=apply)


def override_entitlement(tenant, *, listing: Listing, operator, to_status: str,
                         reason: str, tier_key: str = "", period_end=None,
                         trial_ends_at=None, now=None):
    """The ``entitlement.override`` command: any status to any (spec §6.3 row
    16). The escape hatch for a payment the adapter got wrong; the reason is
    recorded in ``entitlement.overridden``."""
    now = now or timezone.now()

    def apply(actor):
        why = _reason(reason)
        tier = _tier(tenant, tier_key) if tier_key else None
        if to_status == Entitlement.Status.COMPED:
            # A comp is granted, with its own expiry and reason, not overridden.
            raise EntitlementRefused("invalid_state")
        return entitlements.apply_trigger(
            tenant, listing, machine.Trigger.OVERRIDE, now=now, actor=actor,
            tier=tier, operator=operator, reason=why, to_status=to_status,
            period_end=period_end, trial_ends_at=trial_ends_at,
        )

    return _run(tenant, command="entitlement.override", operator=operator, listing=listing,
                payload={"to_status": to_status, "tier": tier_key}, apply=apply)
