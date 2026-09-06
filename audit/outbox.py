"""Writing events to the outbox.

``emit()`` inserts one ``OutboxEvent`` row. It is meant to be called *inside*
the ``transaction.atomic()`` block of a service function, so the event and the
state change it records commit together (spec §11.1). The worker that drains
the outbox to adapters is a later PR.
"""

from __future__ import annotations

from django.utils import timezone

from audit.events import ALL_EVENT_TYPES


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

    return OutboxEvent.all_tenants.create(
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
