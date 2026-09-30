"""The worker's run loop and its single pass.

``worker_pass`` is one iteration, and everything it needs is an argument, so
tests drive it directly:

* it reads the clock once (the caller passes the value) and threads that one
  value through the drain, the mail drain, the CSV import row loop and the tick
  jobs;
* it drains the outbox, drains the mail queue, pushes one bounded chunk of any
  in-progress CSV import, then asks the tick registry to run whichever jobs are
  due. Cadence is per job and lives in the registry (``audit.worker.tick``);
  the pass neither knows nor cares how often a job wants to run. The mail drain
  runs beside the outbox drain, every pass (decisions.md §4.3): a claim-code
  email is not something a directory can wait a tick period for;
* every tick-job failure is reported through ``report``. A failure that is only
  counted is invisible, and a retention job that has been failing for a month
  is a compliance problem nobody was told about;
* it calls ``wait`` only when the pass found nothing to do. A pass that fanned
  out an event, attempted a delivery, sent/retried/dead-lettered/expired a
  mail message, processed an import row, or ran a tick job that did work or has
  more to do goes straight round again -- a backlog drains without sleeping
  between passes. A pass whose only mail activity was claiming *unconfigured*
  rows does not count: those rows are rechecked on their own fixed interval
  (``mail_drain.UNCONFIGURED_RECHECK``), not spun on every pass -- counting them
  here would keep the worker busy-looping, with no ``wait()``, for as long as
  mail stays unconfigured.

``run_loop`` is the ``while True`` around it. It reads the clock once per pass
and is imported by no test. It has no iteration count and no exit condition of
its own: shutdown is a ``KeyboardInterrupt`` raised by the signal handler in
``run_worker``, which propagates straight through here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from audit.worker.drain import drain_once
from audit.worker.mail_drain import mail_drain_once
from audit.worker.tick import TickRegistry
from directory.csv_import import ROWS_PER_PASS
from directory.importing import import_once

# The latency floor when a NOTIFY is missed (listener reconnecting, Postgres
# restarted): the drain still runs every poll interval, selecting on status and
# next_attempt_at, never on a notification. One idle SELECT per second is
# nothing for Postgres and well inside a directory's traffic.
POLL_INTERVAL_SECONDS = 1.0

# The default cadence for a tick job that has no reason to run faster. The spec
# §13 transitions are minute-granular at finest; the heartbeat at 60s is plenty
# to spot a dead worker in the logs.
TICK_PERIOD = timedelta(seconds=60)


def worker_pass(
    *,
    now: datetime,
    ticks: TickRegistry,
    wait: Callable[[], None],
    report: Callable[[str], None],
) -> None:
    """One loop iteration. ``now`` is this pass's clock (a single value).
    ``wait`` is the idle-wait seam -- ``OutboxListener.wait`` in the command,
    a spy in tests. ``report`` receives one line per failed tick job.
    """
    drain = drain_once(now=now)
    mail = mail_drain_once(now=now)
    imp = import_once(now=now, limit=ROWS_PER_PASS)
    tick = ticks.run_due(now=now)

    for failure in tick.failures:
        report(f"osds-worker: tick job failed: {failure}")

    did_work = bool(
        drain.events_fanned_out
        or drain.deliveries_attempted
        # Only rows that reached the provider, or expired, count -- an
        # unconfigured row (claimed but never attempted) does not, or the
        # worker would busy-loop with no wait() for as long as mail stays
        # unconfigured.
        or mail.sent
        or mail.retried
        or mail.dead_lettered
        or mail.expired
        or imp.rows_processed
        or tick.units_done
        or tick.more
    )
    if not did_work:
        wait()


def run_loop(
    *,
    wait: Callable[[], None],
    now: Callable[[], datetime],
    ticks: TickRegistry,
    report: Callable[[str], None],
) -> None:
    """Drain-and-tick forever. ``now`` is a required zero-arg clock callable;
    there is no ``timezone.now`` default here or in ``worker_pass``."""
    while True:
        worker_pass(now=now(), ticks=ticks, wait=wait, report=report)
