"""The outbox drain (spec §8.2, §11.1).

``fan_out_once`` turns every ``pending`` ``OutboxEvent`` into one
``OutboxDelivery`` per subscribed adapter -- zero or more -- and marks the
event ``dispatched``. ``drain_once`` then attempts every due,
head-of-line-clear delivery, applying the retry / dead-letter policy.

**No ``while`` loop lives here.** ``drain_once`` is one pass; a later PR's tick
loop decides cadence.

**Claiming never holds a row lock across a handler.** A claim is a short
transaction -- ``SELECT ... FOR UPDATE SKIP LOCKED``, push ``next_attempt_at``
to a visibility deadline, commit -- after which the handler runs unlocked and
its result is written in a second short transaction. A worker that dies inside
a handler leaves the row ``pending`` with its ``attempt`` count untouched; it
becomes claimable again once the deadline passes. At-least-once: adapters
dedupe on ``event.id``.

``_record`` is a **conditional update**, guarded on ``next_attempt_at`` still
holding the value this claim stamped. A handler that *hangs* past the deadline
(rather than crashing) has its row re-claimed and re-attempted by a later
pass; when the hung call finally returns, its ``_record`` matches nothing and
is discarded -- no double increment, no stale ``retried`` overwriting a
``delivered``.

``BaseException`` -- ``KeyboardInterrupt``, ``SystemExit`` -- is **not**
caught: it propagates out of ``drain_once`` so the shutdown path (PR 3) can
stop cleanly, leaving the claimed row untouched beyond its pushed deadline.

**Ordering** (spec §3.1) is per ``subject``: a delivery is claimable only when
no lower-``id`` ``pending`` delivery exists for the same ``(adapter_id,
subject)``. The check runs on the denormalised ``subject`` column, never a
join to ``outbox_events``. A ``dead`` delivery is not a blocker -- exhaustion
releases the stream.

**Retry** (#172): ``backoff(n) = min(2**(n-1), 3600)`` seconds, full jitter.
1h is a cap, not a term of the sequence. The initial delivery is immediate;
the first retry follows ~1s. ``attempt`` counts *completed* attempts and is
written only after the handler returns. ``attempt >= 12`` dead-letters; a
``permanent`` failure dead-letters at once.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from audit.envelope import to_wire
from audit.models import OutboxDelivery, OutboxEvent
from osds.adapters import Result, subscribers_for
from osds.tenancy import tenant_context

MAX_ATTEMPTS = 12
BACKOFF_CAP_SECONDS = 3600

# Claim visibility deadline. The handler timeout is 30s (spec §8.2); 90s is
# that plus room for the two short claim/record transactions and clock slack
# before another pass may treat the row as abandoned. Short enough that a
# crashed delivery recovers inside two minutes, long enough that a healthy
# 30s handler never races its own deadline.
CLAIM_VISIBILITY_SECONDS = 90

# A single pass fans out at most this many events; a larger backlog drains
# across successive passes.
_FAN_OUT_CAP = 1000

# tenant.* is the one namespace that is not tenant-scoped (spec §3.2): its
# deliveries carry no tenant and its dead letters sit at installation level.
# Mirrors the rule in ``audit.envelope``.
_TENANT_PREFIX = "tenant."


@dataclass(frozen=True)
class FanOutResult:
    events: int = 0
    deliveries: int = 0


@dataclass
class DrainStats:
    events_fanned_out: int = 0
    deliveries_created: int = 0
    deliveries_attempted: int = 0
    delivered: int = 0
    retried: int = 0
    dead_lettered: int = 0
    stale_discarded: int = 0


def backoff(attempt: int) -> timedelta:
    """Delay before the retry that follows the ``attempt``-th completed
    attempt (1-based). ``min(2**(attempt-1), 3600)`` seconds with full jitter:
    ``uniform(0, base)``."""
    base = min(2 ** (attempt - 1), BACKOFF_CAP_SECONDS)
    return timedelta(seconds=random.uniform(0, base))


def _delivery_tenant(event: OutboxEvent):
    return None if event.type.startswith(_TENANT_PREFIX) else event.tenant


def fan_out_once(*, now=None) -> FanOutResult:
    """Create the missing ``OutboxDelivery`` rows for every ``pending`` event
    and mark each event ``dispatched``.

    Idempotent: a re-run after a crash between row creation and the status
    flip finds the rows through ``get_or_create`` and only flips the status.
    An event with no subscribers goes straight to ``dispatched`` with zero
    deliveries.

    ``now`` is the drain pass's clock. A fresh delivery is stamped
    ``next_attempt_at = now`` -- *the same value the pass then claims with* --
    so it is due immediately regardless of clock resolution. ``drain_once``
    always passes it; a standalone caller may omit it and get wall-clock time.
    """
    now = now or timezone.now()
    events = created = 0

    pending = list(
        OutboxEvent.all_tenants.filter(status=OutboxEvent.Status.PENDING)
        .order_by("id")[:_FAN_OUT_CAP]
    )
    for event in pending:
        with transaction.atomic():
            locked = OutboxEvent.all_tenants.select_for_update().get(
                pk=event.pk
            )
            if locked.status != OutboxEvent.Status.PENDING:
                continue
            for subscriber in subscribers_for(locked.type):
                _, made = OutboxDelivery.all_tenants.get_or_create(
                    event=locked,
                    adapter_id=subscriber.id,
                    defaults={
                        "tenant": _delivery_tenant(locked),
                        "subject": locked.subject,
                        "next_attempt_at": now,
                    },
                )
                created += int(made)
            locked.status = OutboxEvent.Status.DISPATCHED
            locked.dispatched_at = now
            locked.save(update_fields=["status", "dispatched_at"])
            events += 1

    return FanOutResult(events, created)


def _claim(now, batch: int) -> "list[int]":
    """Claim up to ``batch`` due, head-of-line-clear deliveries in one short
    transaction and push their ``next_attempt_at`` to the visibility deadline.
    Returns the pks; the rows are unlocked on return."""
    earlier_pending = OutboxDelivery.all_tenants.filter(
        adapter_id=OuterRef("adapter_id"),
        subject=OuterRef("subject"),
        status=OutboxDelivery.Status.PENDING,
        id__lt=OuterRef("id"),
    )
    with transaction.atomic():
        rows = list(
            OutboxDelivery.all_tenants.filter(
                status=OutboxDelivery.Status.PENDING,
                next_attempt_at__lte=now,
            )
            .filter(~Exists(earlier_pending))
            .order_by("id")
            .select_for_update(skip_locked=True)[:batch]
        )
        pks = [row.pk for row in rows]
        if pks:
            OutboxDelivery.all_tenants.filter(pk__in=pks).update(
                next_attempt_at=now
                + timedelta(seconds=CLAIM_VISIBILITY_SECONDS)
            )
    return pks


def _subscriber_for(delivery: OutboxDelivery):
    for subscriber in subscribers_for(delivery.event.type):
        if subscriber.id == delivery.adapter_id:
            return subscriber
    return None


def _record(
    delivery: OutboxDelivery,
    *,
    now,
    status: str,
    attempt: "int | None" = None,
    next_attempt_at=None,
    error: str = "",
) -> bool:
    """Write one attempt's outcome -- but only if the row is still where the
    claim left it. The guard is ``next_attempt_at == delivery.next_attempt_at``:
    this claim stamped that deadline, and a later pass that re-claimed a
    hung handler's row moved it, so a now-stale result matches nothing.

    Returns whether the outcome was applied (``False`` == discarded as stale).
    """
    fields = {
        "status": status,
        "last_attempted_at": now,
        "last_error": error or "",
    }
    if attempt is not None:
        fields["attempt"] = attempt
    if next_attempt_at is not None:
        fields["next_attempt_at"] = next_attempt_at
    if status == OutboxDelivery.Status.DELIVERED:
        fields["delivered_at"] = now

    with transaction.atomic():
        applied = (
            OutboxDelivery.all_tenants.filter(
                pk=delivery.pk,
                next_attempt_at=delivery.next_attempt_at,
            ).update(**fields)
        )
        if applied:
            OutboxDelivery.all_tenants.filter(
                pk=delivery.pk, first_attempted_at__isnull=True
            ).update(first_attempted_at=now)
    return bool(applied)


def attempt_delivery(delivery: OutboxDelivery, *, now) -> str:
    """Run one delivery attempt for an already-claimed row. Returns
    ``"delivered"`` | ``"retried"`` | ``"dead"`` | ``"discarded"`` -- the last
    when the row moved under the handler (a later pass re-claimed it) and the
    now-stale result was dropped.

    ``BaseException`` (``KeyboardInterrupt`` / ``SystemExit``) is left to
    propagate: it is a shutdown, not a failed attempt, and the row keeps its
    ``attempt`` count and its pushed deadline. ``attempt`` is bumped only when
    the handler returns and ``_record`` applies.
    """
    subscriber = _subscriber_for(delivery)
    if subscriber is None:
        applied = _record(
            delivery,
            now=now,
            status=OutboxDelivery.Status.DEAD,
            error="subscriber not registered",
        )
        return "dead" if applied else "discarded"

    envelope = to_wire(delivery.event)
    try:
        result = subscriber.handle(envelope)
    except Exception as exc:  # a returned exception is a retryable failure
        result = Result.failed(f"{type(exc).__name__}: {exc}")

    completed = delivery.attempt + 1

    if result.status in ("ok", "skipped"):
        applied = _record(
            delivery,
            now=now,
            status=OutboxDelivery.Status.DELIVERED,
            attempt=completed,
        )
        return "delivered" if applied else "discarded"

    # result.retry_after_ms is a subscriber hint; the exponential schedule
    # (#172) is what the drain actually applies.
    reason = result.reason or result.status
    permanent = result.status == "failed" and result.permanent
    if permanent or completed >= MAX_ATTEMPTS:
        applied = _record(
            delivery,
            now=now,
            status=OutboxDelivery.Status.DEAD,
            attempt=completed,
            error=reason,
        )
        return "dead" if applied else "discarded"

    applied = _record(
        delivery,
        now=now,
        status=OutboxDelivery.Status.PENDING,
        attempt=completed,
        next_attempt_at=now + backoff(completed),
        error=reason,
    )
    return "retried" if applied else "discarded"


def drain_once(*, now, batch: int = 100) -> DrainStats:
    """One pass: fan out pending events, then attempt every due,
    head-of-line-clear delivery. No loop -- the caller schedules the next
    pass. ``now`` is the pass's clock: it is used to stamp fresh deliveries,
    to select what is due, and as the attempt timestamp, so a pass is
    self-consistent whatever the clock's resolution."""
    stats = DrainStats()

    fanned = fan_out_once(now=now)
    stats.events_fanned_out = fanned.events
    stats.deliveries_created = fanned.deliveries

    for pk in _claim(now, batch):
        delivery = OutboxDelivery.all_tenants.select_related(
            "event", "event__tenant", "tenant"
        ).get(pk=pk)
        with tenant_context(delivery.tenant):
            disposition = attempt_delivery(delivery, now=now)
        stats.deliveries_attempted += 1
        if disposition == "delivered":
            stats.delivered += 1
        elif disposition == "retried":
            stats.retried += 1
        elif disposition == "dead":
            stats.dead_lettered += 1
        else:  # "discarded" -- a later pass owns this row now
            stats.stale_discarded += 1

    return stats
