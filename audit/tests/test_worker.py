"""Worker PR 3: run_worker, the tick loop, LISTEN/NOTIFY with a poll fallback,
the single-instance advisory lock.

``worker_pass`` is pure and is driven directly here; the registry has its own
module (``test_tick_registry``).
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
from audit.worker.mail_drain import MailDrainStats
from audit.worker.singleton import SingleInstanceLock
from audit.worker.tick import TickRegistry, TickStats
from directory.csv_import import ROWS_PER_PASS
from directory.importing import ImportStats

MINUTE = timedelta(seconds=60)


# ----------------------------------------------------------------------------
# worker_pass -- one loop iteration, every seam an argument
# ----------------------------------------------------------------------------
class WorkerPassTests(SimpleTestCase):
    def setUp(self):
        self.drain = self.enterContext(
            mock.patch("audit.worker.loop.drain_once")
        )
        self.mail = self.enterContext(
            mock.patch("audit.worker.loop.mail_drain_once")
        )
        self.imp = self.enterContext(
            mock.patch("audit.worker.loop.import_once")
        )
        self.ticks = mock.Mock(spec=TickRegistry)
        self.ticks.run_due.return_value = TickStats()
        self.report = mock.Mock()
        self.drain.return_value = DrainStats()  # idle unless a test says otherwise
        self.mail.return_value = MailDrainStats()
        self.imp.return_value = ImportStats()

    def _pass(self, *, now, wait):
        return worker_pass(
            now=now, ticks=self.ticks, wait=wait, report=self.report
        )

    def test_the_tick_registry_is_asked_what_is_due_every_pass(self):
        now = timezone.now()
        self._pass(now=now, wait=mock.Mock())
        self.ticks.run_due.assert_called_once_with(now=now)

    def test_a_tick_failure_is_reported_not_dropped(self):
        self.ticks.run_due.return_value = TickStats(
            failures=["sitemaps: RuntimeError: boom"]
        )
        self._pass(now=timezone.now(), wait=mock.Mock())
        self.report.assert_called_once()
        self.assertIn("sitemaps: RuntimeError: boom", self.report.call_args[0][0])

    def test_no_failure_reports_nothing(self):
        self._pass(now=timezone.now(), wait=mock.Mock())
        self.report.assert_not_called()

    def test_a_tick_job_that_did_work_does_not_wait(self):
        self.ticks.run_due.return_value = TickStats(jobs_run=1, units_done=4)
        wait = mock.Mock()
        self._pass(now=timezone.now(), wait=wait)
        wait.assert_not_called()

    def test_a_tick_job_with_backlog_does_not_wait(self):
        self.ticks.run_due.return_value = TickStats(jobs_run=1, more=True)
        wait = mock.Mock()
        self._pass(now=timezone.now(), wait=wait)
        wait.assert_not_called()

    def test_a_tick_that_ran_but_did_nothing_still_waits(self):
        self.ticks.run_due.return_value = TickStats(jobs_run=3)
        wait = mock.Mock()
        self._pass(now=timezone.now(), wait=wait)
        wait.assert_called_once_with()

    def test_idle_pass_waits(self):
        wait = mock.Mock()
        self._pass(now=timezone.now(), wait=wait)
        wait.assert_called_once_with()

    def test_pass_that_attempted_a_delivery_does_not_wait(self):
        self.drain.return_value = DrainStats(deliveries_attempted=3)
        wait = mock.Mock()
        self._pass(now=timezone.now(), wait=wait)
        wait.assert_not_called()

    def test_pass_that_only_fanned_out_does_not_wait(self):
        self.drain.return_value = DrainStats(events_fanned_out=5)
        wait = mock.Mock()
        self._pass(now=timezone.now(), wait=wait)
        wait.assert_not_called()

    def test_pass_that_processed_import_rows_does_not_wait(self):
        self.imp.return_value = ImportStats(rows_processed=5)
        wait = mock.Mock()
        self._pass(now=timezone.now(), wait=wait)
        wait.assert_not_called()

    def test_pass_where_every_claimed_mail_row_was_unconfigured_still_waits(self):
        # Claimed but never attempted -- not work. Unconfigured rows recheck
        # on their own fixed interval (mail_drain.UNCONFIGURED_RECHECK); if
        # this counted as work the worker would busy-loop with no wait() for
        # as long as mail stayed unconfigured.
        self.mail.return_value = MailDrainStats(claimed=3, unconfigured=3)
        wait = mock.Mock()
        self._pass(now=timezone.now(), wait=wait)
        wait.assert_called_once_with()

    def test_pass_that_sent_a_mail_message_does_not_wait(self):
        self.mail.return_value = MailDrainStats(claimed=1, sent=1)
        wait = mock.Mock()
        self._pass(now=timezone.now(), wait=wait)
        wait.assert_not_called()

    def test_pass_that_retried_a_mail_message_does_not_wait(self):
        self.mail.return_value = MailDrainStats(claimed=1, retried=1)
        wait = mock.Mock()
        self._pass(now=timezone.now(), wait=wait)
        wait.assert_not_called()

    def test_pass_that_dead_lettered_a_mail_message_does_not_wait(self):
        self.mail.return_value = MailDrainStats(claimed=1, dead_lettered=1)
        wait = mock.Mock()
        self._pass(now=timezone.now(), wait=wait)
        wait.assert_not_called()

    def test_pass_that_expired_a_mail_message_does_not_wait(self):
        self.mail.return_value = MailDrainStats(claimed=1, expired=1)
        wait = mock.Mock()
        self._pass(now=timezone.now(), wait=wait)
        wait.assert_not_called()

    def test_mail_drain_runs_every_pass_whatever_the_tick_does(self):
        now = timezone.now()
        self._pass(now=now, wait=mock.Mock())
        self._pass(now=now, wait=mock.Mock())
        self.assertEqual(self.mail.call_count, 2)

    def test_one_clock_value_threads_through_drain_mail_import_and_tick(self):
        now = timezone.now()
        self._pass(now=now, wait=mock.Mock())
        self.drain.assert_called_once_with(now=now)
        self.mail.assert_called_once_with(now=now)
        self.imp.assert_called_once_with(now=now, limit=ROWS_PER_PASS)
        self.ticks.run_due.assert_called_once_with(now=now)


# ----------------------------------------------------------------------------
# run_worker command
# ----------------------------------------------------------------------------
class RunWorkerCommandTests(SimpleTestCase):
    def _command(self):
        from audit.management.commands.run_worker import Command

        out, err = io.StringIO(), io.StringIO()
        return Command(stdout=out, stderr=err), out, err

    def test_the_heartbeat_is_registered_and_writes_a_dated_line(self):
        from audit.worker.jobs import build_tick_registry

        out = io.StringIO()
        registry = build_tick_registry(out=out)
        n = timezone.now()
        registry.run_due(now=n)
        self.assertIn("heartbeat", out.getvalue())
        self.assertIn(n.isoformat(), out.getvalue())

    def test_each_handle_builds_its_own_registry(self):
        # #180: two registries never share jobs or bound streams.
        from audit.worker.jobs import build_tick_registry

        first, second = io.StringIO(), io.StringIO()
        build_tick_registry(out=first)
        build_tick_registry(out=second).run_due(now=timezone.now())
        self.assertEqual(first.getvalue(), "")
        self.assertIn("heartbeat", second.getvalue())

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
