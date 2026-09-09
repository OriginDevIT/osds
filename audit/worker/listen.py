"""LISTEN/NOTIFY on ``osds_outbox`` with a polling fallback (spec §11.1).

The listener owns a dedicated psycopg connection (``audit.worker.dbconn``),
never Django's ORM connection -- see that module for why. It issues
``LISTEN osds_outbox`` once and thereafter ``wait()`` blocks on the socket for
up to the poll interval.

The NOTIFY payload is empty by design (``audit.outbox``): a notification means
"scan now", nothing more, and any number arriving between passes collapse to a
single drain. A NOTIFY that never lands -- sent while the listener was
reconnecting, or lost to a Postgres restart -- costs latency only: the next
poll drains the row regardless, because the drain selects on ``status`` and
``next_attempt_at``, never on a notification.

``notifies(timeout=..., stop_after=...)`` needs psycopg >= 3.2; the pin is
3.3.5.
"""

from __future__ import annotations

import psycopg

from audit.outbox import _NOTIFY_CHANNEL as CHANNEL
from audit.worker.dbconn import raw_connection


class OutboxListener:
    def __init__(self, *, poll_interval: float) -> None:
        self._poll_interval = poll_interval
        self._conn: "psycopg.Connection | None" = None

    def start(self) -> None:
        self._conn = raw_connection()
        self._conn.execute(f"LISTEN {CHANNEL}")

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def wait(self) -> None:
        """Block until a NOTIFY arrives or the poll interval elapses. On a
        dropped connection, reconnect and return promptly so the caller's next
        pass is a full drain."""
        if self._conn is None:
            self.start()
            return
        try:
            for _ in self._conn.notifies(
                timeout=self._poll_interval, stop_after=1
            ):
                break  # woken by a notification; the pass that follows drains
        except psycopg.OperationalError:
            # Socket dropped or Postgres restarted. Reconnect; anything NOTIFYed
            # while we were away is caught by the next poll-driven drain.
            self.close()
            try:
                self.start()
            except psycopg.OperationalError:
                pass  # still down -- caller polls again after its next pass
