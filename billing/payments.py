"""Core's side of the payment seam: what it asks a payment provider to do and
when (decisions.md §4.11).

Core knows two capability names, ``payments.checkout`` and
``payments.subscription``, and a provider contract (``osds.adapter_api``); it
never names a vendor. Money stays the provider's: this module asks it for a
checkout URL, asks it to cancel a subscription, asks it for a card-update link,
and applies what comes back through ``billing.reports`` (the inbound route).

``billing.start_checkout`` and ``entitlement.cancel`` follow the orchestrator
shape: ``require_autocommit``, ``log_received``, guards and writes in short
atomic blocks, ``log_conclude``. The provider call is made *outside* any
transaction -- a network round trip must never hold a row lock.

Call with the tenant in ambient scope.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from audit import events
from audit.command_log import log_conclude, log_received, require_autocommit
from audit.outbox import emit
from audit.ratelimit import Rule, check, RateLimited
from billing import entitlements, machine
from billing.models import CheckoutAttempt, Entitlement, Tier
from directory import routing
from directory.models import DirectoryUser, Listing
from osds.adapter_api import CheckoutRequest, PaymentError, SecretNotFound
from osds.adapter_context import build_context
from osds.adapters import capability_provider
from tenants.models import StaffMembership

CAP_CHECKOUT = "payments.checkout"
CAP_SUBSCRIPTION = "payments.subscription"

CHECKOUT_TTL = timedelta(hours=24)
CHECKOUT_RATE = (Rule("billing.checkout.owner.1h", 10, timedelta(hours=1)),)

S = Entitlement.Status


class PaymentRefused(Exception):
    """A guard refusal: nothing written (an attempt that was created is marked
    failed). ``reason`` is a stable code the view maps to copy."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


REFUSAL_TEXT = {
    "unavailable": "Payments are not available on this directory right now.",
    "not_owner": "You do not own this listing.",
    "listing_suspended": "This listing is suspended. Contact the directory.",
    "tier_unknown": "Choose a tier.",
    "tier_not_for_sale": "That tier is not for sale.",
    "already_subscribed": "This listing already has a paid plan.",
    "use_portal": "Update your payment details to fix this; a new purchase is not needed.",
    "not_cancelable": "There is no subscription to cancel.",
    "provider_error": "The payment provider could not complete that. Nothing was changed; please try again.",
    "forbidden": "Your role does not allow this action.",
    "no_portal": "There is no payment page to open for this listing.",
}


def refusal_text(reason: str) -> str:
    return REFUSAL_TEXT.get(reason, "This action was refused.")


# --- the provider ----------------------------------------------------------------


def provider():
    return capability_provider(CAP_CHECKOUT)


def context_for(tenant, *, opener=None):
    p = provider()
    return build_context(tenant, p, opener=opener) if p is not None else None


def available(tenant) -> bool:
    """Whether a customer can be sent to pay: a provider that is configured
    (not that it is reachable) and an absolute HTTPS base for the return URLs
    and the webhook. An HTTP-only install cannot take payments."""
    p = provider()
    if p is None or not routing.has_absolute_base(tenant):
        return False
    try:
        return bool(p.available(build_context(tenant, p)))
    except SecretNotFound:
        return False


# --- start checkout ----------------------------------------------------------------


def _owned_listing(listing: Listing, user: DirectoryUser) -> Listing:
    fresh = Listing.objects.filter(pk=listing.pk).first()
    if fresh is None or fresh.owner_id != user.pk:
        raise PaymentRefused("not_owner")
    if fresh.status == Listing.Status.SUSPENDED:
        raise PaymentRefused("listing_suspended")
    return fresh


def start_checkout(tenant, *, listing: Listing, user: DirectoryUser, tier_key: str, now=None) -> str:
    """The ``billing.start_checkout`` command: an owner asks to buy ``tier_key``
    for their listing. Returns the provider's checkout URL. Raises
    ``PaymentRefused`` or ``RateLimited``."""
    require_autocommit()
    now = now or timezone.now()
    actor = {"type": "owner", "id": user.public_id}

    verdict = check(tenant, checks=[("owner", user.public_id, CHECKOUT_RATE)], now=now)
    if not verdict.allowed:
        if verdict.first_block:
            blocked = log_received(
                command="billing.start_checkout", tenant=tenant, idempotency_key=None,
                actor=actor, trace_id=None, origin="", payload={"listing_id": listing.public_id},
            )
            log_conclude(blocked, outcome="blocked", problem={"rate_limited": verdict.rule})
        raise RateLimited(verdict)

    row = log_received(
        command="billing.start_checkout", tenant=tenant, idempotency_key=None,
        actor=actor, trace_id=None, origin="",
        payload={"listing_id": listing.public_id, "tier": tier_key},
    )
    attempt = None
    try:
        attempt, request, prov = _prepare(tenant, listing=listing, user=user, tier_key=tier_key, now=now)
        try:
            result = prov.create_checkout(build_context(tenant, prov), request)
        except (PaymentError, SecretNotFound) as exc:
            _fail_attempt(attempt)
            raise PaymentRefused("provider_error") from exc
        event_id = _record_started(tenant, attempt, result, actor=actor, now=now)
    except PaymentRefused as exc:
        log_conclude(row, outcome="rejected", problem={"reason": exc.reason})
        raise
    log_conclude(row, outcome="applied", result_event_id=event_id)
    return result.url


@transaction.atomic
def _prepare(tenant, *, listing, user, tier_key, now):
    prov = provider()
    if prov is None or not available(tenant):
        raise PaymentRefused("unavailable")
    fresh = _owned_listing(listing, user)
    tier = Tier.objects.filter(tenant=tenant, key=tier_key).first() if tier_key else None
    if tier is None:
        raise PaymentRefused("tier_unknown")
    if not tier.purchasable or tier.rank == 0 or tier.uses_slot:
        raise PaymentRefused("tier_not_for_sale")

    ent = Entitlement.objects.filter(listing=fresh).first()
    status = ent.status if ent else S.NONE
    if status in (S.PAST_DUE, S.GRACE):
        raise PaymentRefused("use_portal")
    if status in (S.TRIALING, S.ACTIVE):
        raise PaymentRefused("already_subscribed")

    attempt = CheckoutAttempt.objects.create(
        tenant=tenant, listing=fresh, tier=tier, adapter_id=prov.adapter_id,
        started_by=user, expires_at=now + CHECKOUT_TTL,
    )
    base = f"/owner/listings/{fresh.public_id}/billing/return/"
    request = CheckoutRequest(
        attempt_id=attempt.public_id, listing_id=fresh.public_id, listing_name=fresh.name,
        tier_key=tier.key, tier_name=tier.name, price_minor=tier.price_minor,
        currency=tier.currency, interval=tier.interval, trial_days=tier.trial_days,
        customer_email=user.email,
        success_url=routing.absolute_url(tenant, f"{base}?checkout=success"),
        cancel_url=routing.absolute_url(tenant, f"{base}?checkout=cancel"),
    )
    return attempt, request, prov


def _fail_attempt(attempt: CheckoutAttempt) -> None:
    CheckoutAttempt.objects.filter(pk=attempt.pk).update(status=CheckoutAttempt.Status.FAILED)


@transaction.atomic
def _record_started(tenant, attempt, result, *, actor, now) -> str:
    CheckoutAttempt.objects.filter(pk=attempt.pk).update(external_ref=result.external_ref[:200])
    event = emit(
        events.BILLING_CHECKOUT_STARTED,
        subject=attempt.listing.public_id,
        tenant=tenant,
        actor=actor,
        data={
            "plan_id": attempt.tier.key,
            "listing_id": attempt.listing.public_id,
            "amount": attempt.tier.price_minor,
            "currency": attempt.tier.currency,
        },
    )
    return event.event_id


# --- cancel ------------------------------------------------------------------------


def _membership(tenant, operator):
    return StaffMembership.objects.filter(
        operator=operator, tenant=tenant, status=StaffMembership.Status.ACTIVE
    ).first()


def cancel_entitlement(tenant, *, listing: Listing, by: str, user: "DirectoryUser | None" = None,
                       operator=None, reason: str = "", now=None):
    """The ``entitlement.cancel`` command, by the owner (``by="owner"``,
    ``user``) or by a manager (``by="operator"``, ``operator``). The provider is
    asked first: core never marks a subscription cancelled while the provider
    would go on charging it. From grace there is nothing to run to, so it ends
    at once (G3)."""
    require_autocommit()
    now = now or timezone.now()
    if by == "owner":
        actor = {"type": "owner", "id": user.public_id}
        membership = None
    else:
        membership = _membership(tenant, operator)
        kind = "admin" if membership and membership.role >= StaffMembership.Role.ADMIN else "staff"
        actor = {"type": kind, "id": operator.public_id}
    row = log_received(
        command="entitlement.cancel", tenant=tenant, idempotency_key=None, actor=actor,
        trace_id=None, origin="", payload={"listing_id": listing.public_id, "by": by},
    )
    try:
        if by == "owner":
            _owned_listing(listing, user)
        elif membership is None or membership.role < StaffMembership.Role.MANAGER:
            raise PaymentRefused("forbidden")
        ent = Entitlement.objects.filter(listing=listing).first()
        if ent is None or ent.status not in (S.TRIALING, S.ACTIVE, S.PAST_DUE, S.GRACE):
            raise PaymentRefused("not_cancelable")

        if ent.payment_ref:
            prov = provider()
            if prov is None:
                raise PaymentRefused("unavailable")
            try:
                prov.cancel_subscription(
                    build_context(tenant, prov), dict(ent.payment_ref),
                    at_period_end=ent.status != S.GRACE,
                )
            except (PaymentError, SecretNotFound) as exc:
                raise PaymentRefused("provider_error") from exc
        event_id = _apply_cancel(tenant, listing=listing, by=by, reason=reason, actor=actor, now=now)
    except PaymentRefused as exc:
        log_conclude(
            row, outcome="blocked" if exc.reason in ("forbidden", "not_owner") else "rejected",
            problem={"reason": exc.reason},
        )
        raise
    log_conclude(row, outcome="applied", result_event_id=event_id)


@transaction.atomic
def _apply_cancel(tenant, *, listing, by, reason, actor, now) -> str:
    try:
        out = entitlements.apply_trigger(
            tenant, listing, machine.Trigger.CANCEL, now=now, actor=actor,
            canceled_by=by, reason=(reason or "")[:200],
        )
    except machine.InvalidTransition as exc:  # moved under us
        raise PaymentRefused("not_cancelable") from exc
    ent = Entitlement.objects.get(listing=listing)
    event = emit(
        events.BILLING_SUBSCRIPTION_CANCELED,
        subject=listing.public_id, tenant=tenant, actor=actor,
        data={
            "at_period_end": out.to_status == S.CANCELED,
            "reason": (reason or "")[:200],
            "canceled_by": by,
            "listing_id": listing.public_id,
        },
    )
    return event.event_id


def portal_url(tenant, *, listing: Listing, user: DirectoryUser, return_url: str) -> "str | None":
    """The provider's card-update page for this owner's listing, or ``None``."""
    _owned_listing(listing, user)
    ent = Entitlement.objects.filter(listing=listing).first()
    prov = provider()
    if ent is None or not ent.payment_ref or prov is None:
        return None
    try:
        return prov.portal_url(build_context(tenant, prov), dict(ent.payment_ref), return_url)
    except (PaymentError, SecretNotFound):
        raise PaymentRefused("provider_error") from None
