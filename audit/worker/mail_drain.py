"""The mail drain (decisions.md §4.3 "Outbound mail").

Runs every worker pass, beside the outbox drain -- not gated on the tick
period (spec §11's tick jobs are minute-granular; a claim-code email is not).

Claim and record are copied from ``audit.worker.drain`` (§4.3: "the drain
mechanics are copied exactly ... the concurrency hazards do not depend on
consumer count"), not shared, because the state machine differs in three
ways that would otherwise leak into the outbox drain's:

* No per-subject head-of-line ordering. Nothing in §4.3 asks two unrelated
  mail messages to serialize against each other.
* No attempt-count ceiling. ``OutboxDelivery`` dead-letters at 12 attempts;
  here ``expires_at`` is the bound -- the plaintext code or link a message
  carries has its own deadline, and that is what retires a message nobody
  could send in time.
* An "unconfigured" outcome -- no provider registered, or a provider that
  raises ``CapabilityUnconfigured`` -- leaves the row ``pending`` with no
  attempt consumed (§4.3). Treating it as a failure would dead-letter every
  message queued before the operator configured mail. It is rechecked
  ``UNCONFIGURED_RECHECK`` later -- a fixed interval, never ``backoff()``:
  backoff grows with attempts, and this is deliberately not one.

``backoff`` *is* shared with the outbox drain: §4.3 does not say the mail
schedule differs from spec §8.2's, so forking it would be a second copy of
the same sequence to keep in sync for no reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Value
from django.db.models.functions import Coalesce

from audit.models import OutboundMessage
from audit.worker.drain import backoff
from osds.adapter_api import MailMessage
from osds.adapter_context import build_context
from osds.adapters import CapabilityUnconfigured, Result, capability_provider
from osds.tenancy import tenant_context

logger = logging.getLogger("osds.mail")

# Same claim-visibility reasoning as the outbox drain (audit/worker/drain.py):
# the 30s handler timeout (spec §8.2) plus room for the two short claim/record
# transactions and clock slack.
CLAIM_VISIBILITY_SECONDS = 90

# How long an unconfigured row waits before the drain looks at it again. A
# fixed interval, not backoff(): backoff grows with attempt count, and an
# unconfigured row's attempt count never moves (§4.3 -- no attempt consumed),
# so there is nothing for an exponential schedule to key off.
UNCONFIGURED_RECHECK = timedelta(seconds=60)

_CLAIM_BATCH = 100
_CAPABILITY = "email.send"


@dataclass
class MailDrainStats:
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    dead_lettered: int = 0
    expired: int = 0
    unconfigured: int = 0
    stale_discarded: int = 0


def _claim(now, batch: int) -> "list[int]":
    """Claim up to ``batch`` due pending messages in one short transaction
    and push their ``next_attempt_at`` to the visibility deadline. Returns
    the pks; the rows are unlocked on return."""
    with transaction.atomic():
        rows = list(
            OutboundMessage.all_tenants.filter(
                status=OutboundMessage.Status.PENDING,
                next_attempt_at__lte=now,
            )
            .order_by("id")
            .select_for_update(skip_locked=True)[:batch]
        )
        pks = [row.pk for row in rows]
        if pks:
            OutboundMessage.all_tenants.filter(pk__in=pks).update(
                next_attempt_at=now + timedelta(seconds=CLAIM_VISIBILITY_SECONDS)
            )
    return pks


def _record(
    message: OutboundMessage,
    *,
    now,
    status: str,
    attempt: "int | None" = None,
    next_attempt_at=None,
    error: str = "",
    sent: bool = False,
) -> bool:
    """Write one attempt's outcome in a single ``UPDATE`` -- but only if the
    row is still where the claim left it (guarded on ``next_attempt_at``,
    same reasoning as ``audit.worker.drain._record``: a hung handler's row
    re-claimed by a later pass makes this write match nothing, and it is
    discarded). ``first_attempted_at`` rides along in the same statement via
    ``Coalesce`` -- stamped with ``now`` only while still null, left alone
    otherwise -- so recording one outcome costs exactly one query, not a
    fields update followed by a separate first-attempt check."""
    fields = {
        "status": status,
        "last_attempted_at": now,
        "last_error": error or "",
        "first_attempted_at": Coalesce("first_attempted_at", Value(now)),
    }
    if attempt is not None:
        fields["attempt"] = attempt
    if next_attempt_at is not None:
        fields["next_attempt_at"] = next_attempt_at
    if status in (
        OutboundMessage.Status.SENT,
        OutboundMessage.Status.DEAD,
        OutboundMessage.Status.EXPIRED,
    ):
        fields["body_text"] = None
    if sent:
        fields["sent_at"] = now

    applied = OutboundMessage.all_tenants.filter(
        pk=message.pk,
        next_attempt_at=message.next_attempt_at,
    ).update(**fields)
    return bool(applied)


def attempt_message(message: OutboundMessage, *, now) -> str:
    """Run one delivery attempt for an already-claimed row. Returns
    ``"sent"`` | ``"retried"`` | ``"dead"`` | ``"expired"`` |
    ``"unconfigured"`` | ``"discarded"`` -- the last when the row moved
    under the handler and the now-stale result was dropped.
    """
    if now >= message.expires_at:
        applied = _record(message, now=now, status=OutboundMessage.Status.EXPIRED)
        return "expired" if applied else "discarded"

    provider = capability_provider(_CAPABILITY)
    if provider is None:
        applied = _record(
            message,
            now=now,
            status=OutboundMessage.Status.PENDING,
            next_attempt_at=now + UNCONFIGURED_RECHECK,
            error="no email.send provider registered",
        )
        return "unconfigured" if applied else "discarded"

    try:
        # The provider gets a context and plain data -- never the ORM row
        # and never the tenant (#215).
        result = provider.send(
            build_context(message.tenant, provider),
            MailMessage(
                message_id=message.message_id,
                kind=message.kind,
                to_address=message.to_address or "",
                subject=message.subject,
                body_text=message.body_text or "",
            ),
        )
    except CapabilityUnconfigured as exc:
        applied = _record(
            message,
            now=now,
            status=OutboundMessage.Status.PENDING,
            next_attempt_at=now + UNCONFIGURED_RECHECK,
            error=str(exc) or "email.send is not configured",
        )
        return "unconfigured" if applied else "discarded"
    except Exception as exc:  # a returned exception is a retryable failure
        result = Result.failed(type(exc).__name__)

    completed = message.attempt + 1

    if result.status == "ok":
        applied = _record(
            message,
            now=now,
            status=OutboundMessage.Status.SENT,
            attempt=completed,
            sent=True,
        )
        return "sent" if applied else "discarded"

    reason = result.reason or result.status
    if result.status == "failed" and result.permanent:
        applied = _record(
            message,
            now=now,
            status=OutboundMessage.Status.DEAD,
            attempt=completed,
            error=reason,
        )
        return "dead" if applied else "discarded"

    applied = _record(
        message,
        now=now,
        status=OutboundMessage.Status.PENDING,
        attempt=completed,
        next_attempt_at=now + backoff(completed),
        error=reason,
    )
    return "retried" if applied else "discarded"


def mail_drain_once(*, now, batch: int = _CLAIM_BATCH) -> MailDrainStats:
    """One pass: claim every due pending message and attempt it. No loop --
    the caller (``audit.worker.loop.worker_pass``) schedules the next pass."""
    stats = MailDrainStats()

    for pk in _claim(now, batch):
        stats.claimed += 1
        message = OutboundMessage.all_tenants.select_related("tenant").get(pk=pk)
        with tenant_context(message.tenant):
            disposition = attempt_message(message, now=now)
        if disposition == "sent":
            stats.sent += 1
        elif disposition == "retried":
            stats.retried += 1
        elif disposition == "dead":
            stats.dead_lettered += 1
        elif disposition == "expired":
            stats.expired += 1
        elif disposition == "unconfigured":
            stats.unconfigured += 1
        else:  # "discarded" -- a later pass owns this row now
            stats.stale_discarded += 1

    # One aggregate line, never one per row (§4.3: "the drain logs once per
    # pass") -- a backlog queued before the operator configures mail must
    # not turn into a log line per pending message every second. WARNING,
    # not INFO: this project ships no LOGGING config, so the default root
    # level (WARNING) would silently swallow an INFO record -- an operator
    # needs this line to actually reach the worker's stdout/stderr with no
    # configuration of their own.
    if stats.unconfigured:
        logger.warning(
            "osds-mail-drain: %d message(s) pending, email.send unconfigured",
            stats.unconfigured,
        )

    return stats
