"""A command-log conclude failure after a successful apply must not turn a
durable write into a thrown error. The row stays at outcome=NULL, which is the
key a reconciler closes it on."""

from __future__ import annotations

from unittest import mock

from django.db import DatabaseError

from audit import command_log
from audit.models import CommandLog, OutboxEvent
from directory.tests.test_upsert_listing import _Base


def _fail_applied(real):
    def wrapper(row, *, outcome, **kw):
        if outcome == "applied":
            raise DatabaseError("conclude write failed")
        return real(row, outcome=outcome, **kw)

    return wrapper


class ConcludeFailureAfterApplyTests(_Base):
    def test_upsert_returns_result_when_conclude_fails(self):
        real = command_log.log_conclude
        fail = _fail_applied(real)
        with mock.patch("audit.command_log.log_conclude", fail), \
                mock.patch("directory.services.log_conclude", fail), \
                self.assertLogs("osds.command_log", level="ERROR") as logs:
            result = self.upsert(
                {"slug": "acme-co", "name": "Acme Co"}, idempotency_key="k-97"
            )
        self.assertEqual(result.outcome, "created")
        self.assertTrue(result.event_id)
        self.assertTrue(
            OutboxEvent.all_tenants.filter(type="listing.created").exists()
        )
        row = CommandLog.all_tenants.get(idempotency_key="k-97")
        self.assertIsNone(row.outcome)
        self.assertIsNone(row.concluded_at)
        self.assertIn(str(row.pk), "\n".join(logs.output))
