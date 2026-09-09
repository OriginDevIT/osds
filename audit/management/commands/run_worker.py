"""Run the outbox worker: drain deliveries, run the tick jobs, forever.

One instance per installation, guarded by a Postgres advisory lock. LISTEN on
``osds_outbox`` for latency, poll every second as the reliable fallback
(spec §11.1). The adapter registry is empty until block 5, so today the drain
fans events out to nothing -- which is all CSV import needs from the worker.

Shutdown contract, for the ``docker-compose.yml`` this repo does not yet have:

* The stop signal is SIGTERM. The handler below turns it into
  ``KeyboardInterrupt`` -- the same exception SIGINT already raises -- which
  ``drain_once`` / ``tick_once`` let through untouched (#174). An in-flight
  adapter handler is abandoned, not recorded as a failed attempt: its delivery
  row was claimed with ``next_attempt_at`` pushed only 90s
  (``CLAIM_VISIBILITY_SECONDS``) ahead and is redelivered after that.
* Clean shutdown -- unwind the loop, release the advisory lock, close the
  connections -- is sub-second. It does NOT wait for the in-flight handler.
* Therefore a SIGKILL at any moment (grace period exceeded, ``docker kill``,
  power loss) is safe. ``stop_grace_period`` needs only ~2s and MUST NOT be
  set on the assumption that the worker finishes claimed work before exiting.
  There is no lower bound tied to the 30s handler timeout or the 90s deadline.
* The worker container needs ``DATABASE_URL`` in its environment at start (for
  the lock connection), and ``restart: unless-stopped`` so a lock-contention
  exit is retried until Postgres reaps the previous backend.
"""

from __future__ import annotations

import signal
import sys

from django.core.management.base import BaseCommand
from django.utils import timezone

from audit.worker.listen import OutboxListener
from audit.worker.loop import POLL_INTERVAL_SECONDS, TICK_PERIOD, run_loop
from audit.worker.singleton import ADVISORY_LOCK_KEY, SingleInstanceLock
from audit.worker.tick import register_tick_job


def _raise_keyboard_interrupt(signum, frame):
    # signal handlers are called with (signum, frame); we need neither.
    raise KeyboardInterrupt


class Command(BaseCommand):
    help = "Run the outbox worker (drain + tick loop). One instance per install."

    def handle(self, *args, **options):
        lock = SingleInstanceLock()
        if not lock.acquire():
            self.stderr.write(
                f"osds-worker: another instance holds advisory lock "
                f"{ADVISORY_LOCK_KEY}; exiting."
            )
            sys.exit(1)
        self.stdout.write(
            f"osds-worker: acquired advisory lock {ADVISORY_LOCK_KEY}; starting."
        )

        # SIGTERM -> KeyboardInterrupt (SIGINT already raises it, so no handler
        # for that). The loop lets it propagate; the finally below is the whole
        # of graceful shutdown -- see the module docstring for the compose
        # stop_grace_period contract.
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

        register_tick_job("heartbeat", self._heartbeat)

        listener = OutboxListener(poll_interval=POLL_INTERVAL_SECONDS)
        listener.start()
        try:
            run_loop(
                wait=listener.wait, now=timezone.now, tick_period=TICK_PERIOD
            )
        except KeyboardInterrupt:
            self.stdout.write("osds-worker: shutdown signal received; stopping.")
        finally:
            listener.close()
            lock.release()
            self.stdout.write("osds-worker: stopped.")

    def _heartbeat(self, *, now) -> None:
        self.stdout.write(f"osds-worker: heartbeat {now.isoformat()}")
