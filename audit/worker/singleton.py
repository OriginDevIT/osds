"""Single-instance guard: a session-level Postgres advisory lock.

Two workers would double every delivery attempt and race the tick jobs. At
boot the worker takes ``pg_try_advisory_lock`` on a fixed key; a second worker
gets ``False`` at once, logs, and exits non-zero.

The lock is session-scoped -- Postgres releases it when the holding connection
closes, which covers a clean exit, a crash, and a SIGKILL once the backend is
reaped. So it rides its own dedicated connection (``audit.worker.dbconn``),
never Django's ORM connection: that one is recycled between drain passes and
reconnected on error, and the lock would quietly release the first time that
happened while the worker kept running.

The "advisory locks: rejected" line in ``docs/decisions.md`` is about slot
capacity allocation, where the row must *be* the lock. A process singleton is
an unrelated use of the primitive.
"""

from __future__ import annotations

import psycopg

from audit.worker.dbconn import raw_connection

# Frozen, arbitrary, permanent -- treated like an event-name constant. Signed
# 64-bit range; well under 2**63.
ADVISORY_LOCK_KEY = 8_112_000_000_000_001


class SingleInstanceLock:
    def __init__(self) -> None:
        self._conn: "psycopg.Connection | None" = None
        self.held = False

    def acquire(self) -> bool:
        """Try to take the lock. Returns whether this process now holds it;
        on failure the connection is closed again."""
        self._conn = raw_connection()
        row = self._conn.execute(
            "SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_KEY]
        ).fetchone()
        self.held = bool(row[0])
        if not self.held:
            self.release()
        return self.held

    def release(self) -> None:
        """Release the lock and close the connection. Safe to call twice, and
        safe to call after a failed ``acquire``."""
        if self._conn is None:
            return
        try:
            if self.held:
                self._conn.execute(
                    "SELECT pg_advisory_unlock(%s)", [ADVISORY_LOCK_KEY]
                )
        except psycopg.Error:
            pass  # closing the connection releases the lock anyway
        finally:
            self.held = False
            self._conn.close()
            self._conn = None
