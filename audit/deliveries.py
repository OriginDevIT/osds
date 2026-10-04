"""An adapter's deliveries: the status page's queries, and replay
(spec §8.2, decisions.md §4.12).

``recent`` and ``health`` read ``OutboxDelivery`` for one tenant and one adapter.
Nothing here shows a payload or an endpoint: a delivery is an event type, a
status, a count and an error class.

``replay_delivery`` is the admin's ``delivery.replay`` command: it puts a
dead-lettered delivery back in the queue. It refuses an event whose payload has
been nulled (spec §11.2, 90 days): a replay would send an empty ``data`` and call
it the event. Ordering is untouched -- the replayed row keeps its id, so the
drain's head-of-line rule holds later events for the same subject behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from audit.command_log import log_conclude, log_received, require_autocommit
from audit.models import OutboxDelivery
from tenants.models import StaffMembership

REPLAY_ROLE = StaffMembership.Role.ADMIN
WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class Health:
    pending: int          # waiting, never attempted
    retrying: int         # attempted and failed, will try again
    dead: int             # gave up; replayable
    delivered: int        # in the window
    last_delivered_at: "object | None"
    last_error: str       # the latest failure's error class or status
    failing: bool         # something is retrying or dead right now


def _base(tenant, adapter_id):
    return OutboxDelivery.all_tenants.filter(tenant=tenant, adapter_id=adapter_id)


def recent(tenant, adapter_id, *, status=None, limit=50):
    qs = _base(tenant, adapter_id).select_related("event")
    if status:
        qs = qs.filter(status=status)
    return list(qs.order_by("-id")[:limit])


def health(tenant, adapter_id, *, now=None) -> Health:
    now = now or timezone.now()
    qs = _base(tenant, adapter_id)
    pending_qs = qs.filter(status=OutboxDelivery.Status.PENDING)
    dead = qs.filter(status=OutboxDelivery.Status.DEAD).count()
    retrying = pending_qs.filter(attempt__gt=0).count()
    last_ok = (
        qs.filter(status=OutboxDelivery.Status.DELIVERED)
        .order_by("-delivered_at").values_list("delivered_at", flat=True).first()
    )
    last_error = (
        qs.exclude(last_error="").order_by("-last_attempted_at")
        .values_list("last_error", flat=True).first() or ""
    )
    return Health(
        pending=pending_qs.filter(attempt=0).count(),
        retrying=retrying,
        dead=dead,
        delivered=qs.filter(status=OutboxDelivery.Status.DELIVERED, delivered_at__gte=now - WINDOW).count(),
        last_delivered_at=last_ok,
        last_error=last_error,
        failing=bool(retrying or dead),
    )


class ReplayRefused(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


REFUSAL_TEXT = {
    "forbidden": "Only an admin can replay a delivery.",
    "not_found": "That delivery does not exist.",
    "not_dead": "Only a delivery that gave up can be replayed.",
    "payload_nulled": (
        "This event's details were removed after 90 days (spec section 11.2), so "
        "replaying it would send an empty event."
    ),
}


def replay_delivery(tenant, *, adapter_id: str, event_id: str, operator, now=None) -> OutboxDelivery:
    require_autocommit()
    membership = StaffMembership.objects.filter(
        operator=operator, tenant=tenant, status=StaffMembership.Status.ACTIVE
    ).first()
    role = membership.role if membership else -1
    row = log_received(
        command="delivery.replay", tenant=tenant, idempotency_key=None,
        actor={"type": "admin" if role >= StaffMembership.Role.ADMIN else "staff", "id": operator.public_id},
        trace_id=None, origin="", payload={"adapter_id": adapter_id, "event_id": event_id},
    )
    try:
        delivery = _apply_replay(tenant, adapter_id=adapter_id, event_id=event_id, role=role, now=now)
    except ReplayRefused as exc:
        log_conclude(row, outcome="blocked" if exc.reason == "forbidden" else "rejected",
                     problem={"reason": exc.reason})
        raise
    log_conclude(row, outcome="applied")
    return delivery


@transaction.atomic
def _apply_replay(tenant, *, adapter_id, event_id, role, now):
    if role < REPLAY_ROLE:
        raise ReplayRefused("forbidden")
    delivery = (
        OutboxDelivery.all_tenants.select_for_update()
        .select_related("event")
        .filter(tenant=tenant, adapter_id=adapter_id, event__event_id=event_id)
        .first()
    )
    if delivery is None:
        raise ReplayRefused("not_found")
    if delivery.status != OutboxDelivery.Status.DEAD:
        raise ReplayRefused("not_dead")
    if delivery.event.payload_nulled_at is not None:
        raise ReplayRefused("payload_nulled")
    delivery.status = OutboxDelivery.Status.PENDING
    delivery.attempt = 0
    delivery.next_attempt_at = now or timezone.now()
    delivery.last_error = ""
    delivery.delivered_at = None
    delivery.save(update_fields=["status", "attempt", "next_attempt_at", "last_error", "delivered_at"])
    return delivery
