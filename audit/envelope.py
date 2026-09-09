"""The wire form of an event.

``to_wire`` renders an ``OutboxEvent`` row to the dict a subscriber's
``handle`` receives (spec §2). The one transform core owes the transport:
``tenant.*`` events carry no ``tenant`` block -- ``OsdsAnyEvent`` is
``OsdsEvent | TenantEvent`` (spec §8), and on a ``tenant.*`` event the tenant
*is* the subject, not the context.

Any absolute URL that belongs in a payload is built by ``directory.routing``
(``absolute_url``), never ``reverse()`` -- the worker has no per-request
urlconf (#122). ``to_wire`` itself only copies ``data`` through; URL
construction happens in the service that emits.
"""

from __future__ import annotations

from datetime import timezone as _dt_timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from audit.models import OutboxEvent

_TENANT_PREFIX = "tenant."


def _rfc3339(dt) -> str:
    """RFC 3339, UTC, millisecond precision (spec §2)."""
    return (
        dt.astimezone(_dt_timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def to_wire(event: "OutboxEvent") -> dict:
    """The §2 envelope for ``event``. Omits ``tenant`` for ``tenant.*`` types."""
    envelope = {
        "id": event.event_id,
        "type": event.type,
        "version": event.version,
        "occurred_at": _rfc3339(event.occurred_at),
        "subject": event.subject,
        "actor": event.actor or {},
        "origin": event.origin or None,
        "trace_id": event.trace_id or event.event_id,
        "data": event.data or {},
    }
    if not event.type.startswith(_TENANT_PREFIX):
        envelope["tenant"] = {
            "id": event.tenant.public_id,
            "slug": event.tenant.slug,
            "domain": event.tenant.primary_domain or None,
        }
    return envelope
