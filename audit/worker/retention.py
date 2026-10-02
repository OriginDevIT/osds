"""90-day retention sweeps (spec §11.2): the audit side.

Each is a tick job. ``now`` is required and keyword-only, every job is
idempotent (a row already nulled no longer matches its own filter), and every
job is bounded: ``RETENTION_CHUNK`` rows per call, oldest first, with
``more=True`` while a full chunk came back so a backlog clears at pass speed
without one long UPDATE standing in front of the mail drain.

All of them are cross-tenant and read through ``all_tenants``. None needs a
tenant in scope: they do not read
tenant data, they blank it.

What nulling means here:

* ``OutboundMessage.to_address`` -> ``NULL`` (#213). Only rows in a terminal
  status. A pending row is still going to be sent, and the drain must never find
  its address gone.
* ``OutboxEvent.data`` -> ``{}`` with ``payload_nulled_at`` stamped. The column
  is ``NOT NULL`` by design of the envelope (``audit.envelope.to_wire`` already
  serialises ``data or {}``), and the stamp is what distinguishes "nulled" from
  "never had a payload". Only events with nothing left to deliver: not still
  awaiting fan-out, and with no ``pending`` delivery. Dead letters are *not*
  exempt (decisions.md §4, "Dead letters are not exempt from payload nulling").
* ``CommandLog.payload`` -> ``NULL``, the one permitted rewrite of a concluded
  command-log row. Nothing else on the row is touched.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Exists, OuterRef

from audit.models import (
    CommandLog,
    OutboundMessage,
    OutboxDelivery,
    OutboxEvent,
    RateLimitCounter,
)
from audit.worker.tick import TickResult

RETENTION = timedelta(days=90)

# Rows per call. Small enough that one UPDATE is short, large enough that a
# year of backlog is a few hundred passes.
RETENTION_CHUNK = 1000


def _first_ids(queryset) -> "list[int]":
    return list(queryset.order_by("id").values_list("id", flat=True)[:RETENTION_CHUNK])


def null_outbound_addresses(*, now) -> TickResult:
    """#213: clear ``to_address`` on terminal messages older than 90 days."""
    terminal = [
        OutboundMessage.Status.SENT,
        OutboundMessage.Status.DEAD,
        OutboundMessage.Status.EXPIRED,
    ]
    ids = _first_ids(
        OutboundMessage.all_tenants.filter(
            created_at__lt=now - RETENTION,
            to_address__isnull=False,
            status__in=terminal,
        )
    )
    done = OutboundMessage.all_tenants.filter(
        id__in=ids, to_address__isnull=False, status__in=terminal
    ).update(to_address=None)
    return TickResult(done=done, more=len(ids) == RETENTION_CHUNK)


def null_event_payloads(*, now) -> TickResult:
    """Spec §11.2: blank ``OutboxEvent.data`` for events older than 90 days."""
    has_pending_delivery = Exists(
        OutboxDelivery.all_tenants.filter(
            event=OuterRef("pk"), status=OutboxDelivery.Status.PENDING
        )
    )
    due = (
        OutboxEvent.all_tenants.filter(
            created_at__lt=now - RETENTION, payload_nulled_at__isnull=True
        )
        .exclude(status=OutboxEvent.Status.PENDING)
        .annotate(has_pending_delivery=has_pending_delivery)
        .filter(has_pending_delivery=False)
    )
    ids = _first_ids(due)
    done = OutboxEvent.all_tenants.filter(
        id__in=ids, payload_nulled_at__isnull=True
    ).update(data={}, payload_nulled_at=now)
    return TickResult(done=done, more=len(ids) == RETENTION_CHUNK)


def null_command_payloads(*, now) -> TickResult:
    """Spec §11.2: blank ``CommandLog.payload`` for commands older than 90 days.

    Includes a row whose command threw mid-apply (``concluded_at`` null): its
    payload is the same second copy of personal data."""
    ids = _first_ids(
        CommandLog.all_tenants.filter(
            received_at__lt=now - RETENTION, payload__isnull=False
        )
    )
    done = CommandLog.all_tenants.filter(
        id__in=ids, payload__isnull=False
    ).update(payload=None)
    return TickResult(done=done, more=len(ids) == RETENTION_CHUNK)


# A rate-limit window is at most 24 hours (``directory.claim_limits``), so a
# counter whose window began more than twice that ago can never be read again.
RATE_LIMIT_KEEP = timedelta(hours=48)


def prune_rate_limit_counters(*, now) -> TickResult:
    """#210: delete dead rate-limit counters. Not a §11.2 retention rule -- the
    counters hold only keyed hashes -- but the same shape: bounded, idempotent,
    run from the tick."""
    ids = _first_ids(
        RateLimitCounter.all_tenants.filter(window_start__lt=now - RATE_LIMIT_KEEP)
    )
    done, _ = RateLimitCounter.all_tenants.filter(id__in=ids).delete()
    return TickResult(done=done, more=len(ids) == RETENTION_CHUNK)
