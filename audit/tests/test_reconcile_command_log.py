"""The command-log reconciler closes a null-outcome row only when the command's
event is durable. A row left by a command that threw mid-apply has no event and
stays the way it is."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.db import DatabaseError
from django.utils import timezone

from audit import command_log
from audit.models import CommandLog, OutboxEvent
from audit.worker import reconcile
from directory.tests.test_upsert_listing import _Base


def _fail_applied(real):
    def wrapper(row, *, outcome, **kw):
        if outcome == "applied":
            raise DatabaseError("conclude write failed")
        return real(row, outcome=outcome, **kw)

    return wrapper


class ReconcileTests(_Base):
    def _upsert_with_failed_conclude(self, key="k-97", **payload):
        fail = _fail_applied(command_log.log_conclude)
        with mock.patch("audit.command_log.log_conclude", fail), \
                mock.patch("directory.services.log_conclude", fail), \
                self.assertLogs("osds.command_log", level="ERROR"):
            return self.upsert(
                {"slug": "acme-co", "name": "Acme Co", **payload}, idempotency_key=key
            )

    def _later(self, minutes=10):
        return timezone.now() + timedelta(minutes=minutes)

    def test_closes_a_null_outcome_row_whose_event_is_durable(self):
        result = self._upsert_with_failed_conclude()
        row = CommandLog.all_tenants.get(idempotency_key="k-97")
        self.assertIsNone(row.outcome)

        done = reconcile.reconcile_command_log(now=self._later())

        row.refresh_from_db()
        self.assertEqual(done.done, 1)
        self.assertEqual(row.outcome, "applied")
        self.assertEqual(row.result_event_id, result.event_id)
        self.assertIsNotNone(row.concluded_at)

    def test_leaves_a_row_alone_when_the_command_threw_mid_apply(self):
        with mock.patch(
            "directory.services._apply_upsert", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.upsert({"slug": "acme-co", "name": "Acme Co"}, idempotency_key="k-x")
        self.assertFalse(OutboxEvent.all_tenants.exists())

        done = reconcile.reconcile_command_log(now=self._later())

        row = CommandLog.all_tenants.get(idempotency_key="k-x")
        self.assertEqual(done.done, 0)
        self.assertIsNone(row.outcome)
        self.assertIsNone(row.concluded_at)

    def test_another_commands_event_does_not_close_a_failed_row(self):
        # One durable-but-unconcluded row and one that threw: only the first closes.
        self._upsert_with_failed_conclude(key="k-ok")
        with mock.patch(
            "directory.services._apply_upsert", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.upsert({"slug": "b", "name": "B"}, idempotency_key="k-bad")

        reconcile.reconcile_command_log(now=self._later())

        self.assertEqual(CommandLog.all_tenants.get(idempotency_key="k-ok").outcome, "applied")
        self.assertIsNone(CommandLog.all_tenants.get(idempotency_key="k-bad").outcome)

    def test_a_row_inside_the_grace_window_is_left_for_its_own_command(self):
        self._upsert_with_failed_conclude()

        done = reconcile.reconcile_command_log(now=timezone.now())

        self.assertEqual(done.done, 0)
        self.assertIsNone(CommandLog.all_tenants.get(idempotency_key="k-97").outcome)

    def test_a_concluded_row_is_never_rewritten_and_the_job_is_idempotent(self):
        self._upsert_with_failed_conclude()
        reconcile.reconcile_command_log(now=self._later())
        row = CommandLog.all_tenants.get(idempotency_key="k-97")
        concluded_at = row.concluded_at

        again = reconcile.reconcile_command_log(now=self._later(20))

        row.refresh_from_db()
        self.assertEqual(again.done, 0)
        self.assertEqual(row.concluded_at, concluded_at)

    def test_retry_with_the_same_key_returns_the_original_event(self):
        first = self._upsert_with_failed_conclude()
        reconcile.reconcile_command_log(now=self._later())

        retry = self.upsert(
            {"slug": "acme-co", "name": "Acme Co"}, idempotency_key="k-97"
        )

        self.assertEqual(retry.outcome, "replayed")
        self.assertEqual(retry.event_id, first.event_id)
        self.assertEqual(self.events("listing.created").count(), 1)

    def test_the_job_is_registered_on_the_worker_tick(self):
        import io
        from audit.worker.jobs import build_tick_registry

        names = [j.name for j in build_tick_registry(out=io.StringIO()).jobs]
        self.assertIn("command_log_reconcile", names)
