"""Writing events to the outbox.

``emit()`` inserts one ``OutboxEvent`` row inside the caller's
``transaction.atomic()`` block, so the event and the state change it records
commit together (spec §11.1). On commit it fires a ``pg_notify`` on
``osds_outbox`` to wake the worker; the payload is empty because the worker
re-scans the outbox regardless -- the notify is a latency hint, not a carrier.
On rollback the notify is discarded with everything else.
"""

from __future__ import annotations

from django.db import connection, transaction
from django.utils import timezone

from audit.events import ALL_EVENT_TYPES

_NOTIFY_CHANNEL = "osds_outbox"


def _notify_outbox() -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_notify(%s, %s)", [_NOTIFY_CHANNEL, ""])


def emit(
    event_type: str,
    *,
    subject: str,
    tenant,
    data: dict | None = None,
    actor: dict | None = None,
    origin: str = "",
    trace_id: str = "",
    version: int = 1,
):
    if event_type not in ALL_EVENT_TYPES:
        raise ValueError(f"unknown event type: {event_type!r}")

    from audit.models import OutboxEvent

    event = OutboxEvent.all_tenants.create(
        type=event_type,
        version=version,
        occurred_at=timezone.now(),
        subject=subject,
        tenant=tenant,
        actor=actor or {},
        origin=origin or "",
        trace_id=trace_id or "",
        data=data or {},
    )
    transaction.on_commit(_notify_outbox)
    return event
