"""The command log (spec §11.2): every command *attempted*, including the
rejected and blocked ones.

The log is written **outside** the command transaction. ``log_received``
records the attempt before that transaction opens; ``log_conclude`` records
the outcome after it settles; each commits on its own. A command that throws
mid-apply leaves a row with a null ``outcome`` -- that is the record, not a
gap. A concluded row is never rewritten.

These helpers do no transaction management of their own: the caller must be in
autocommit so each write lands independently of the command it logs.
``require_autocommit`` enforces that, raising ``MustNotBeInTransaction``.

This is the home of the helpers shared by every service layer that speaks in
commands -- ``directory.services`` today, ``tenants.services`` next. See
``audit.models.CommandLog`` for the row shape and ``audit.outbox`` for the
sibling event-log writer.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from audit.models import CommandLog


class MustNotBeInTransaction(RuntimeError):
    """A command orchestrator was called inside an open transaction. Its
    command-log rows are committed independently of the command transaction
    (spec §11.2); a caller-opened transaction would pull them in and lose the
    very guarantee the log exists for. Loop over independent calls -- do not
    wrap a batch in one transaction.
    """


def require_autocommit() -> None:
    """Raise ``MustNotBeInTransaction`` if a transaction is already open.

    Call this at the top of a command orchestrator, before the received row is
    written: that row has to survive a rollback of the command it logs, which
    it cannot do from inside the caller's transaction.
    """
    if transaction.get_connection().in_atomic_block:
        raise MustNotBeInTransaction()


def log_received(
    *,
    command: str,
    tenant: Any,
    idempotency_key: "str | None",
    actor: "dict | None",
    trace_id: "str | None",
    origin: str,
    payload: Any,
) -> CommandLog:
    """Record a command attempt before its transaction opens.

    Returns the unconcluded row; hand it to ``log_conclude`` once the command
    has settled. ``tenant`` may be ``None`` -- a malformed command that never
    resolved one still has to leave a trace (spec §11.2). ``origin`` is the
    originating adapter id and is stored as ``adapter_id``.
    """
    return CommandLog.objects.create(
        command=command,
        tenant=tenant,
        idempotency_key=idempotency_key or None,
        adapter_id=origin or "",
        actor=actor or {},
        trace_id=trace_id or "",
        payload=payload,
    )


def log_conclude(
    row: CommandLog,
    *,
    outcome: str,
    result_event_id: "str | None" = None,
    problem: "dict | None" = None,
) -> None:
    """Write the outcome of a command onto its received row.

    ``outcome`` is one of ``CommandLog.Outcome`` (``applied`` / ``rejected`` /
    ``blocked``). A row this is never called on keeps ``outcome`` and
    ``concluded_at`` null -- the "threw mid-apply" record.
    """
    row.outcome = outcome
    row.result_event_id = result_event_id or ""
    row.problem = problem
    row.concluded_at = timezone.now()
    row.save(
        update_fields=["outcome", "result_event_id", "problem", "concluded_at"]
    )


def log_replay(
    *,
    command: str,
    tenant: Any,
    idempotency_key: "str | None",
    actor: "dict | None",
    trace_id: "str | None",
    prior: CommandLog,
) -> None:
    """Record an idempotent replay: a command whose idempotency key already has
    an applied row. Written already concluded, carrying the prior result's
    event id and ``problem={"idempotent_replay": True}``.
    """
    now = timezone.now()
    CommandLog.objects.create(
        command=command,
        tenant=tenant,
        idempotency_key=idempotency_key or None,
        actor=actor or {},
        trace_id=trace_id or "",
        payload=None,
        outcome="applied",
        result_event_id=prior.result_event_id or "",
        problem={"idempotent_replay": True},
        received_at=now,
        concluded_at=now,
    )
