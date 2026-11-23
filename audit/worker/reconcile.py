"""Command-log reconciler (spec §11.2, #97).

A command's row is concluded *after* its transaction commits. If that write
fails (``audit.command_log.log_conclude_applied`` swallows the error), the row
is left at ``outcome`` NULL and ``concluded_at`` NULL -- the same state a
command that threw mid-apply leaves. The two are told apart by the event: a
command that committed wrote its outbox event in the same transaction as its
state change, tagged with the row's primary key (``OutboxEvent.command_log_id``,
set by ``audit.command_log.applying``). A row with a tagged event is durable and
is closed ``applied``; a row with none is left exactly as it is.

This job is the only thing that closes a row it did not open, so it is narrow:

* it keys on the event link alone -- never on timing, the idempotency key, or
  a similar-looking event -- so a command that genuinely failed is never marked
  concluded;
* it skips rows received within ``GRACE``, which may belong to a command whose
  own conclude write has not run yet;
* its write is conditional on the row still being unconcluded, so it can never
  overwrite a conclusion, and a second run finds nothing to do.

A command that committed no event (an unchanged upsert, a cooldown) has nothing
to prove durability with and stays at NULL.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Exists, OuterRef

from audit import events
from audit.models import CommandLog, OutboxEvent
from audit.worker.retention import RETENTION_CHUNK
from audit.worker.tick import TickResult

GRACE = timedelta(minutes=5)

# A command that emits several events names the one it returns. Every other
# command emits one event for the row.
_RESULT_TYPE = {"claim.submit": events.CLAIM_SUBMITTED}


def _result_event_id(row: CommandLog, tagged: "list[OutboxEvent]") -> str:
    preferred = _RESULT_TYPE.get(row.command)
    for event in tagged:
        if event.type == preferred:
            return event.event_id
    return tagged[-1].event_id


def reconcile_command_log(*, now) -> TickResult:
    durable = OutboxEvent.all_tenants.filter(command_log_id=OuterRef("pk"))
    rows = list(
        CommandLog.all_tenants.filter(
            outcome__isnull=True,
            concluded_at__isnull=True,
            received_at__lt=now - GRACE,
        )
        .filter(Exists(durable))
        .order_by("id")[:RETENTION_CHUNK]
    )
    done = 0
    for row in rows:
        tagged = list(
            OutboxEvent.all_tenants.filter(command_log_id=row.pk).order_by("id")
        )
        done += CommandLog.all_tenants.filter(
            pk=row.pk, outcome__isnull=True, concluded_at__isnull=True
        ).update(
            outcome=CommandLog.Outcome.APPLIED,
            result_event_id=_result_event_id(row, tagged),
            concluded_at=now,
        )
    return TickResult(done=done, more=len(rows) == RETENTION_CHUNK)
