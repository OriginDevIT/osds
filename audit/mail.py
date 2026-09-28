"""Enqueuing outbound mail (decisions.md §4.3 "Outbound mail").

``enqueue()`` inserts one ``OutboundMessage`` row inside the caller's
``transaction.atomic()`` block. The mail ruling's guarantee is atomicity with
the causing state change: a stored verification-code hash with no message,
or a message with no hash, is a broken claim. This is the inverse of
``directory.services.upsert_listing``'s guard (``audit.command_log
.require_autocommit``) -- there the caller must NOT be in a transaction; here
it must already be in one, enforced the same way, for the same reason: the
write has to land in the right transaction, and a mistake in either direction
is silent without the check.

Sending emits no event and writes no command log (§4.3) -- the causing
command already emits and logs, and delivery state lives entirely on the
``OutboundMessage`` row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from audit.models import OutboundMessage

if TYPE_CHECKING:
    from tenants.models import Tenant


class MustBeInTransaction(RuntimeError):
    """``enqueue`` was called outside an open transaction. The row it writes
    must commit atomically with the state change that caused it -- a crash
    between the two leaves a hash with no message, or a message with no
    hash, and either is a broken claim."""


def require_atomic() -> None:
    """Raise ``MustBeInTransaction`` unless a transaction is already open."""
    if not transaction.get_connection().in_atomic_block:
        raise MustBeInTransaction()


def enqueue(
    *,
    tenant: "Tenant",
    kind: str,
    to_address: str,
    subject: str,
    body_text: str,
    expires_at=None,
) -> OutboundMessage:
    """Queue one outbound email. Must be called inside the caller's
    ``transaction.atomic()`` block -- see ``require_atomic``.

    ``expires_at`` is the deadline of the code or link ``body_text`` carries,
    computed by the caller (spec §9.6); ``None`` means no deadline.
    """
    require_atomic()
    return OutboundMessage.all_tenants.create(
        tenant=tenant,
        kind=kind,
        to_address=to_address,
        subject=subject,
        body_text=body_text,
        expires_at=expires_at,
        next_attempt_at=timezone.now(),
    )
