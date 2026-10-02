"""``entitlement.reportPayment``: a payment adapter reports an outcome and core
decides what it means (spec §6, §7; decisions.md §4.11).

The adapter says *what happened* (a checkout completed, an invoice was paid or
failed, a refund, a cancellation); it never says what the tier becomes. Core
maps the report to a trigger on the state machine, applies it, and emits the
``billing.*`` event for the money fact and the ``entitlement.*`` and
``listing.tier_changed`` events for the consequence. Core emits all of them: an
adapter never emits (spec §8.3). Each carries the adapter's id as ``origin``,
which is what keeps an adapter from reacting to its own report.

Idempotent on the provider's event id: ``PaymentReceipt`` is unique on
``(tenant, adapter, event id)`` and is inserted in the same transaction as the
state change, so a redelivery -- two at once included -- applies once. An event
core cannot or need not act on (an unknown checkout, a move the state machine
refuses) is acknowledged and recorded ``ignored``: the provider is told it was
received and does not retry what retrying cannot fix.

Call with the tenant in ambient scope.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit import events
from audit.command_log import log_conclude, log_received, log_replay, require_autocommit
from audit.models import CommandLog
from audit.outbox import emit
from billing import entitlements, machine
from billing.machine import Trigger
from billing.models import CheckoutAttempt, Entitlement, PaymentReceipt, Tier
from directory.models import Listing
from osds.adapter_api import REPORT_KINDS, PaymentReport

COMMAND = "entitlement.reportPayment"
S = Entitlement.Status


class ReportRefused(Exception):
    """A report core will not apply. ``reason`` is a stable code."""

    def __init__(self, reason: str, *, attempt_pk: "int | None" = None):
        self.reason = reason
        # A purchase already in force: the checkout attempt is still finished,
        # and that write has to survive the rollback of this one.
        self.attempt_pk = attempt_pk
        super().__init__(reason)


@dataclass
class ReportResult:
    outcome: str  # "applied" | "replayed" | "ignored"
    reason: str = ""


def _summary(report: PaymentReport) -> dict:
    """What the command log keeps of a report: ids and the kind. No address, no
    payment detail, no provider reference."""
    return {
        "kind": report.kind,
        "listing_id": report.listing_id,
        "checkout_attempt_id": report.checkout_attempt_id,
        "tier": report.tier_key,
    }


def report_payment(tenant, *, adapter_id: str, report: PaymentReport, now=None) -> ReportResult:
    require_autocommit()
    now = now or timezone.now()
    key = f"{adapter_id}:{report.external_event_id}"
    actor = {"type": "adapter", "id": adapter_id}

    prior = (
        CommandLog.objects.filter(
            command=COMMAND, tenant=tenant, idempotency_key=key, outcome="applied"
        ).order_by("id").first()
    )
    if prior is not None:
        log_replay(command=COMMAND, tenant=tenant, idempotency_key=key, actor=actor,
                   trace_id=None, prior=prior)
        return ReportResult("replayed")
    if PaymentReceipt.objects.filter(
        tenant=tenant, adapter_id=adapter_id, external_event_id=report.external_event_id
    ).exists():
        return ReportResult("replayed")  # an ignored event, seen before

    row = log_received(
        command=COMMAND, tenant=tenant, idempotency_key=key, actor=actor,
        trace_id=None, origin=adapter_id, payload=_summary(report),
    )
    try:
        event_id = _apply(tenant, adapter_id=adapter_id, report=report, actor=actor, now=now)
    except IntegrityError:
        # Another delivery of the same event won the receipt.
        log_conclude(row, outcome="applied", problem={"idempotent_replay": True})
        return ReportResult("replayed")
    except ReportRefused as exc:
        log_conclude(row, outcome="rejected", problem={"reason": exc.reason})
        _ignore(tenant, adapter_id=adapter_id, report=report, reason=exc.reason)
        if exc.attempt_pk is not None:
            CheckoutAttempt.objects.filter(
                pk=exc.attempt_pk, status=CheckoutAttempt.Status.STARTED
            ).update(status=CheckoutAttempt.Status.COMPLETED, completed_at=now)
        return ReportResult("ignored", exc.reason)
    log_conclude(row, outcome="applied", result_event_id=event_id)
    return ReportResult("applied")


def _ignore(tenant, *, adapter_id, report, reason) -> None:
    try:
        with transaction.atomic():
            PaymentReceipt.objects.create(
                tenant=tenant, adapter_id=adapter_id, kind=report.kind[:40],
                external_event_id=report.external_event_id[:200],
                outcome=PaymentReceipt.Outcome.IGNORED, reason=reason[:60],
            )
    except IntegrityError:
        pass


# --- resolution ---------------------------------------------------------------


def _resolve(tenant, report: PaymentReport):
    """The listing and the checkout attempt (if any) this report is about."""
    attempt = None
    if report.checkout_attempt_id:
        attempt = (
            CheckoutAttempt.objects.select_related("listing", "tier")
            .filter(public_id=report.checkout_attempt_id).first()
        )
        if attempt is None:
            raise ReportRefused("unknown_checkout")
    listing = None
    if report.listing_id:
        listing = Listing.objects.filter(public_id=report.listing_id).first()
        if listing is None:
            raise ReportRefused("unknown_listing")
    if attempt is not None:
        if listing is not None and listing.pk != attempt.listing_id:
            raise ReportRefused("checkout_mismatch")
        listing = attempt.listing
    if listing is None:
        raise ReportRefused("unknown_listing")
    return listing, attempt


def _tier(tenant, key: str) -> "Tier | None":
    if not key:
        return None
    tier = Tier.objects.filter(tenant=tenant, key=key).first()
    if tier is None:
        raise ReportRefused("unknown_tier")
    return tier


def _trigger(listing, ent, attempt, report: PaymentReport):
    """Map the report to ``(trigger, params, billing_event, billing_data)``."""
    kind = report.kind
    status = ent.status if ent else S.NONE
    ref = dict(report.payment_ref or {})
    cur = report.currency or (attempt.tier.currency if attempt else "")

    if kind == "checkout_completed":
        if attempt is None:
            raise ReportRefused("unknown_checkout")
        if attempt.status != CheckoutAttempt.Status.STARTED:
            raise ReportRefused("checkout_already_completed")
        tier = attempt.tier
        if report.trial_ends_at is not None:
            trigger, params = Trigger.START_TRIAL, {"tier": tier, "trial_ends_at": report.trial_ends_at}
        else:
            if report.period_end is None:
                raise ReportRefused("bad_report")
            trigger, params = Trigger.START, {"tier": tier, "period_end": report.period_end}
        params["payment_ref"] = ref
        data = {"subscription": ref.get("external_id", ""), "plan": tier.key,
                "current_period_end": (report.period_end or report.trial_ends_at).isoformat()}
        return trigger, params, events.BILLING_SUBSCRIPTION_STARTED, data

    if kind == "payment_succeeded":
        if report.period_end is None:
            raise ReportRefused("bad_report")
        data = {"amount": report.amount_minor, "currency": cur, "invoice_ref": report.invoice_ref}
        if status in (S.NONE, S.EXPIRED):
            # The first invoice can arrive before the checkout completion.
            if attempt is None or attempt.status != CheckoutAttempt.Status.STARTED:
                raise ReportRefused("no_entitlement")
            params = {"tier": attempt.tier, "period_end": report.period_end, "payment_ref": ref}
            return Trigger.START, params, events.BILLING_PAYMENT_SUCCEEDED, data
        params = {"period_end": report.period_end, "payment_ref": ref}
        if report.tier_key:
            params["tier"] = _tier(listing.tenant, report.tier_key)
        return Trigger.PAYMENT_SUCCEEDED, params, events.BILLING_PAYMENT_SUCCEEDED, data

    if kind == "payment_failed":
        data = {
            "attempt": report.attempt, "failure_code": report.failure_code,
            "next_retry_at": report.next_retry_at.isoformat() if report.next_retry_at else None,
        }
        params = {"failure_code": report.failure_code, "attempt": report.attempt}
        return Trigger.PAYMENT_FAILED, params, events.BILLING_PAYMENT_FAILED, data

    if kind == "refunded":
        data = {"amount": report.amount_minor, "currency": cur, "reason": report.reason,
                "issued_by": "provider"}
        return Trigger.REFUND, {}, events.BILLING_REFUND_ISSUED, data

    if kind == "subscription_changed":
        if not report.tier_key or report.period_end is None:
            raise ReportRefused("bad_report")
        new = _tier(listing.tenant, report.tier_key)
        data = {"from_plan": ent.tier.key if ent else None, "to_plan": new.key, "proration": None}
        params = {"tier": new, "period_end": report.period_end, "payment_ref": ref}
        return Trigger.PAYMENT_SUCCEEDED, params, events.BILLING_SUBSCRIPTION_CHANGED, data

    if kind == "subscription_canceled":
        data = {"at_period_end": report.at_period_end, "reason": report.reason,
                "canceled_by": report.canceled_by or "provider"}
        if report.at_period_end:
            params = {"canceled_by": report.canceled_by or "owner", "reason": report.reason}
            return Trigger.CANCEL, params, events.BILLING_SUBSCRIPTION_CANCELED, data
        return Trigger.SUBSCRIPTION_ENDED, {}, events.BILLING_SUBSCRIPTION_CANCELED, data

    raise ReportRefused("unknown_kind")  # pragma: no cover - guarded below


@transaction.atomic
def _apply(tenant, *, adapter_id, report: PaymentReport, actor, now) -> str:
    if report.kind not in REPORT_KINDS:
        raise ReportRefused("unknown_kind")
    if not report.external_event_id:
        raise ReportRefused("bad_report")

    # The receipt goes in first: a concurrent delivery of the same event fails
    # here, before any state change.
    receipt = PaymentReceipt.objects.create(
        tenant=tenant, adapter_id=adapter_id, kind=report.kind[:40],
        external_event_id=report.external_event_id[:200],
        outcome=PaymentReceipt.Outcome.APPLIED,
    )
    listing, attempt = _resolve(tenant, report)
    ent = Entitlement.objects.filter(listing=listing).first()
    trigger, params, billing_event, billing_data = _trigger(listing, ent, attempt, report)

    try:
        entitlements.apply_trigger(
            tenant, listing, trigger, now=now, actor=actor, origin=adapter_id, **params
        )
    except (machine.InvalidTransition, entitlements.EntitlementError) as exc:
        invalid = isinstance(exc, machine.InvalidTransition)
        done = (
            attempt.pk
            if invalid and attempt is not None
            and report.kind in ("checkout_completed", "payment_succeeded")
            else None
        )
        raise ReportRefused(
            "invalid_transition" if invalid else "bad_report", attempt_pk=done
        ) from exc

    if attempt is not None and trigger in (Trigger.START, Trigger.START_TRIAL):
        CheckoutAttempt.objects.filter(pk=attempt.pk).update(
            status=CheckoutAttempt.Status.COMPLETED, completed_at=now,
            external_ref=attempt.external_ref or (report.payment_ref or {}).get("external_id", "")[:200],
        )
    receipt.listing = listing
    receipt.save(update_fields=["listing"])

    event = emit(
        billing_event,
        subject=listing.public_id, tenant=tenant, actor=actor, origin=adapter_id,
        data={"listing_id": listing.public_id, **billing_data},
    )
    return event.event_id
