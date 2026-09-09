"""The worker's run loop and its single pass.

``worker_pass`` is one iteration, and everything it needs is an argument, so
tests drive it directly:

* it reads the clock once (the caller passes the value) and threads that one
  value through the drain, the tick-due check and the tick jobs;
* it drains the outbox, then runs the tick jobs if ``tick_period`` has elapsed
  since ``last_tick``, and returns the new ``last_tick``;
* it calls ``wait`` only when the pass found nothing to do. A pass that fanned
  out an event or attempted a delivery goes straight round again -- a backlog
  drains without sleeping between passes.

``run_loop`` is the ``while True`` around it. It holds ``last_tick``, reads the
clock once per pass, and is imported by no test. It has no iteration count and
no exit condition of its own: shutdown is a ``KeyboardInterrupt`` raised by the
signal handler in ``run_worker``, which propagates straight through here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from audit.worker.drain import drain_once
from audit.worker.tick import tick_once

# The latency floor when a NOTIFY is missed (listener reconnecting, Postgres
# restarted): the drain still runs every poll interval, selecting on status and
# next_attempt_at, never on a notification. One idle SELECT per second is
# nothing for Postgres and well inside a directory's traffic.
POLL_INTERVAL_SECONDS = 1.0

# How often the tick jobs run. The spec §13 transitions are minute-granular at
# finest; the heartbeat at 60s is plenty to spot a dead worker in the logs.
TICK_PERIOD = timedelta(seconds=60)


def worker_pass(*, now, last_tick, tick_period, wait) -> datetime:
    """One loop iteration. ``now`` is this pass's clock (a single value).
    ``wait`` is the idle-wait seam -- ``OutboxListener.wait`` in the command,
    a spy in tests. Returns the tick time to carry into the next pass.
    """
    stats = drain_once(now=now)

    new_last_tick = last_tick
    if last_tick is None or now - last_tick >= tick_period:
        tick_once(now=now)
        new_last_tick = now

    claimed_work = bool(stats.events_fanned_out or stats.deliveries_attempted)
    if not claimed_work:
        wait()
    return new_last_tick


def run_loop(*, wait, now, tick_period=TICK_PERIOD) -> None:
    """Drain-and-tick forever. ``now`` is a required zero-arg clock callable;
    there is no ``timezone.now`` default here or in ``worker_pass``."""
    last_tick: "datetime | None" = None
    while True:
        last_tick = worker_pass(
            now=now(),
            last_tick=last_tick,
            tick_period=tick_period,
            wait=wait,
        )
