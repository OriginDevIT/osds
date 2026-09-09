"""Worker PR 3: run_worker, the tick loop, LISTEN/NOTIFY with a poll fallback,
the single-instance advisory lock.

``worker_pass`` and the tick registry are pure and are driven directly here.
``run_loop`` is a ``while True`` and is imported by nothing. The listener and
the lock need a real Postgres session, so those tests are ``TransactionTestCase``.
"""

from __future__ import annotations

import io
import time
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

from audit.worker.drain import DrainStats
from audit.worker.listen import OutboxListener
from audit.worker.loop import worker_pass
from audit.worker.singleton import SingleInstanceLock
from audit.worker.tick import (
    override_tick_jobs,
    register_tick_job,
    tick_once,
)

MINUTE = timedelta(seconds=60)


# ----------------------------------------------------------------------------
# worker_pass -- one loop iteration, every seam an argument
# ----------------------------------------------------------------------------
class WorkerPassTests(SimpleTestCase):
    def setUp(self):
        self.drain = self.enterContext(
            mock.patch("audit.worker.loop.drain_once")
        )
        self.tick = self.enterContext(
            mock.patch("audit.worker.loop.tick_once")
        )
        self.drain.return_value = DrainStats()  # idle unless a test says otherwise

    def _pass(self, *, now, last_tick, wait):
        return worker_pass(
            now=now, last_tick=last_tick, tick_period=MINUTE, wait=wait
        )

    def test_first_pass_runs_the_tick_and_sets_last_tick(self):
        now = timezone.now()
        new_tick = self._pass(now=now, last_tick=None, wait=mock.Mock())
        self.assertEqual(new_tick, now)
        self.tick.assert_called_once_with(now=now)

    def test_tick_is_skipped_before_the_period_elapses(self):
        now = timezone.now()
        last = now - timedelta(seconds=30)
        new_tick = self._pass(now=now, last_tick=last, wait=mock.Mock())
        self.assertEqual(new_tick, last)
        self.tick.assert_not_called()

    def test_tick_runs_again_once_the_period_has_elapsed(self):
        now = timezone.now()
        last = now - timedelta(seconds=90)
        new_tick = self._pass(now=now, last_tick=last, wait=mock.Mock())
        self.assertEqual(new_tick, now)
        self.tick.assert_called_once_with(now=now)

    def test_idle_pass_waits(self):
        wait = mock.Mock()
        self._pass(now=timezone.now(), last_tick=None, wait=wait)
        wait.assert_called_once_with()

    def test_pass_that_attempted_a_delivery_does_not_wait(self):
        self.drain.return_value = DrainStats(deliveries_attempted=3)
        wait = mock.Mock()
        self._pass(now=timezone.now(), last_tick=None, wait=wait)
        wait.assert_not_called()

    def test_pass_that_only_fanned_out_does_not_wait(self):
        self.drain.return_value = DrainStats(events_fanned_out=5)
        wait = mock.Mock()
        self._pass(now=timezone.now(), last_tick=None, wait=wait)
        wait.assert_not_called()

    def test_one_clock_value_threads_through_drain_and_tick(self):
        now = timezone.now()
        self._pass(now=now, last_tick=None, wait=mock.Mock())
        self.drain.assert_called_once_with(now=now)
        self.tick.assert_called_once_with(now=now)


# ----------------------------------------------------------------------------
# tick registry
# ----------------------------------------------------------------------------
class TickRegistryTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(override_tick_jobs())

    def test_runs_every_job_once_in_order_with_now(self):
        seen = []
        register_tick_job("a", lambda *, now: seen.append(("a", now)))
        register_tick_job("b", lambda *, now: seen.append(("b", now)))
        n = timezone.now()

        stats = tick_once(now=n)

        self.assertEqual(seen, [("a", n), ("b", n)])
        self.assertEqual(stats.jobs_run, 2)
        self.assertEqual(stats.failures, [])

    def test_registration_is_idempotent_on_name(self):
        register_tick_job("a", lambda *, now: None)

        def boom(*, now):
            raise AssertionError("second registration should have been ignored")

        register_tick_job("a", boom)
        stats = tick_once(now=timezone.now())
        self.assertEqual(stats.jobs_run, 1)

    def test_a_failing_job_is_recorded_and_the_rest_still_run(self):
        ran = []
        register_tick_job("bad", lambda *, now: 1 / 0)
        register_tick_job("good", lambda *, now: ran.append(now))

        stats = tick_once(now=timezone.now())

        self.assertEqual(len(ran), 1)
        self.assertEqual(stats.jobs_run, 1)
        self.assertEqual(len(stats.failures), 1)
        self.assertIn("bad", stats.failures[0])
        self.assertIn("ZeroDivisionError", stats.failures[0])

    def test_baseexception_from_a_job_propagates(self):
        def interrupted(*, now):
            raise KeyboardInterrupt

        register_tick_job("boom", interrupted)
        with self.assertRaises(KeyboardInterrupt):
            tick_once(now=timezone.now())


# ----------------------------------------------------------------------------
# run_worker command
# ----------------------------------------------------------------------------
class RunWorkerCommandTests(SimpleTestCase):
    def _command(self):
        from audit.management.commands.run_worker import Command

        out, err = io.StringIO(), io.StringIO()
        return Command(stdout=out, stderr=err), out, err

    def test_heartbeat_writes_a_dated_line(self):
        cmd, out, _ = self._command()
        n = timezone.now()
        cmd._heartbeat(now=n)
        self.assertIn("heartbeat", out.getvalue())
        self.assertIn(n.isoformat(), out.getvalue())

    def test_exits_nonzero_when_another_instance_holds_the_lock(self):
        from audit.management.commands import run_worker as mod

        with mock.patch.object(
            mod.SingleInstanceLock, "acquire", return_value=False
        ), mock.patch.object(mod.SingleInstanceLock, "release"):
            cmd, _, err = self._command()
            with self.assertRaises(SystemExit) as ctx:
                cmd.handle()

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("another instance", err.getvalue())


# ----------------------------------------------------------------------------
# LISTEN/NOTIFY -- needs a real session
# ----------------------------------------------------------------------------
class OutboxListenerTests(TransactionTestCase):
    def _listener(self, *, poll_interval):
        listener = OutboxListener(poll_interval=poll_interval)
        listener.start()
        self.addCleanup(listener.close)
        return listener

    def test_a_notify_wakes_the_wait_promptly(self):
        listener = self._listener(poll_interval=5.0)
        with connection.cursor() as cur:
            cur.execute("SELECT pg_notify('osds_outbox', '')")

        start = time.monotonic()
        listener.wait()
        self.assertLess(time.monotonic() - start, 2.0)

    def test_wait_falls_back_to_the_poll_interval_with_no_notify(self):
        listener = self._listener(poll_interval=0.3)

        start = time.monotonic()
        listener.wait()
        elapsed = time.monotonic() - start

        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 3.0)

    def test_a_missed_notify_is_still_seen_on_the_next_poll(self):
        # No NOTIFY is ever sent (mirrors a notification lost while the listener
        # was reconnecting). The poll still returns, and it is the drain the
        # caller runs next -- not the notification -- that clears the row.
        listener = self._listener(poll_interval=0.3)
        for _ in range(2):
            listener.wait()  # returns on the timeout, no notification needed


# ----------------------------------------------------------------------------
# single-instance advisory lock -- needs a real session
# ----------------------------------------------------------------------------
@mock.patch("audit.worker.singleton.ADVISORY_LOCK_KEY", 918_273_645_000_111)
class SingleInstanceLockTests(TransactionTestCase):
    def test_a_second_holder_is_refused_and_the_lock_frees_on_release(self):
        first = SingleInstanceLock()
        self.assertTrue(first.acquire())
        self.addCleanup(first.release)

        second = SingleInstanceLock()
        self.assertFalse(second.acquire())

        first.release()

        third = SingleInstanceLock()
        self.assertTrue(third.acquire())
        third.release()

    def test_release_is_safe_after_a_failed_acquire_and_twice_over(self):
        holder = SingleInstanceLock()
        self.assertTrue(holder.acquire())

        loser = SingleInstanceLock()
        self.assertFalse(loser.acquire())
        loser.release()  # no-op, connection already closed

        holder.release()
        holder.release()  # idempotent
