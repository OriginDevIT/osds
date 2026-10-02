"""Tick jobs owned by billing (spec §13 "scheduled jobs"; decisions.md §4.7,
§4.11).

Registered by ``audit.worker.jobs.build_tick_registry``, never at import time.
Cross-tenant by nature, so they read through ``all_tenants`` and enter each
tenant explicitly.

``entitlement_transitions`` is the clock's half of the state machine: dunning
to grace after 14 days, grace to expired after 30, a cancelled period's end, a
comp's expiry, a term's end, and a trial the adapter never reported on. It
selects the rows that look due, then lets ``apply_trigger`` re-check each under
its row lock -- a payment that landed a moment ago wins, and the job skips the
row. Idempotent, and bounded: one chunk a call, ``more`` while there is
backlog.
"""

from __future__ import annotations

from django.db.models import Q

from audit import events
from audit.outbox import emit
from audit.worker.tick import TickResult
from billing import entitlements, machine
from billing.machine import Trigger
from billing.models import Entitlement
from osds.tenancy import tenant_context

CHUNK = 200

S = Entitlement.Status


def _due_q(now) -> Q:
    return (
        Q(status=S.PAST_DUE, dunning_ends_at__lte=now)
        | Q(status=S.GRACE, grace_ends_at__lte=now)
        | Q(status=S.CANCELED, current_period_end__lte=now)
        | Q(status=S.COMPED, comp_expires_at__lte=now)
        | Q(status=S.ACTIVE, billing_mode=Entitlement.BillingMode.TERM, current_period_end__lte=now)
        | Q(status=S.TRIALING, trial_ends_at__lte=now - machine.TRIAL_OVERDUE)
    )


def _trigger_for(ent: Entitlement, now) -> "str | None":
    for trigger in machine.CLOCK_TRIGGERS:
        if machine.is_due(ent, trigger, now):
            return trigger
    return None


def entitlement_transitions(*, now) -> TickResult:
    rows = list(
        Entitlement.all_tenants.filter(_due_q(now))
        .select_related("tenant", "listing", "tier")
        .order_by("id")[:CHUNK]
    )
    done = 0
    for ent in rows:
        trigger = _trigger_for(ent, now)
        if trigger is None:
            continue
        with tenant_context(ent.tenant):
            try:
                entitlements.apply_trigger(ent.tenant, ent.listing, trigger, now=now)
            except machine.NotDue:
                continue  # moved under us
        done += 1
    return TickResult(done=done, more=len(rows) == CHUNK)


def renewal_notices(*, now) -> TickResult:
    """Spec §6.6: ten days before a term entitlement ends, tell the incumbent
    the renewal window is open. Emits ``entitlement.renewal_due`` once a
    period. Terms are not purchasable until slots exist, so today this covers
    an operator-set term; the owner mail rides with the payment PR."""
    rows = list(
        Entitlement.all_tenants.filter(
            status=S.ACTIVE,
            billing_mode=Entitlement.BillingMode.TERM,
            renewal_notified_at__isnull=True,
            current_period_end__gt=now,
            current_period_end__lte=now + machine.RENEWAL_NOTICE,
        )
        .select_related("tenant", "listing", "tier")
        .order_by("id")[:CHUNK]
    )
    done = 0
    for ent in rows:
        with tenant_context(ent.tenant):
            if _notify_renewal(ent, now):
                done += 1
    return TickResult(done=done, more=len(rows) == CHUNK)


def _notify_renewal(ent: Entitlement, now) -> bool:
    from django.db import transaction

    with transaction.atomic():
        locked = Entitlement.objects.select_for_update().filter(pk=ent.pk).first()
        if (
            locked is None
            or locked.status != S.ACTIVE
            or locked.renewal_notified_at is not None
            or locked.current_period_end is None
            or locked.current_period_end <= now
        ):
            return False
        locked.renewal_notified_at = now
        locked.save(update_fields=["renewal_notified_at", "updated_at"])
        emit(
            events.ENTITLEMENT_RENEWAL_DUE,
            subject=locked.public_id,
            tenant=ent.tenant,
            actor=entitlements.SYSTEM_ACTOR,
            data={
                "entitlement_id": locked.public_id,
                "listing_id": ent.listing.public_id,
                "days_remaining": max(0, (locked.current_period_end - now).days),
                "term_days": locked.term_days,
            },
        )
    return True
