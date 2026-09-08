"""audit.command_log -- the shared writers for the command log (spec §11.2).

Two things are pinned here:

* the row each helper writes matches the shape documented on
  ``audit.models.CommandLog``;
* ``log_received`` commits on its own, with no wrapping transaction, so the
  received row survives a rollback of the command it logs.

The autocommit behaviour needs ``TransactionTestCase`` -- a plain ``TestCase``
wraps every test in a transaction, which is the situation the helpers are
written to stay out of.
"""

from __future__ import annotations

from django.db import transaction
from django.test import TestCase, TransactionTestCase

from audit.command_log import (
    MustNotBeInTransaction,
    log_conclude,
    log_received,
    log_replay,
    require_autocommit,
)
from audit.models import CommandLog
from tenants.models import Tenant

ACTOR = {"type": "admin", "id": "op_test"}
PAYLOAD = {"slug": "acme-co", "name": "Acme Co"}


class LogReceivedRowShapeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def test_row_matches_the_documented_shape(self):
        row = log_received(
            command="listing.upsert",
            tenant=self.tenant,
            idempotency_key="csv:row-1",
            actor=ACTOR,
            trace_id="trace-abc",
            origin="adapter_x",
            payload=PAYLOAD,
        )

        self.assertIsInstance(row, CommandLog)
        self.assertIsNotNone(row.pk)

        stored = CommandLog.objects.get(pk=row.pk)
        self.assertEqual(stored.command, "listing.upsert")
        self.assertEqual(stored.tenant, self.tenant)
        self.assertEqual(stored.idempotency_key, "csv:row-1")
        self.assertEqual(stored.adapter_id, "adapter_x")  # origin -> adapter_id
        self.assertEqual(stored.actor, ACTOR)
        self.assertEqual(stored.trace_id, "trace-abc")
        self.assertEqual(stored.payload, PAYLOAD)
        # An unconcluded row: outcome and its companions are still empty.
        self.assertIsNone(stored.outcome)
        self.assertEqual(stored.result_event_id, "")
        self.assertIsNone(stored.problem)
        self.assertIsNone(stored.concluded_at)
        # received_at is defaulted, not passed.
        self.assertIsNotNone(stored.received_at)

    def test_empty_inputs_are_normalised(self):
        row = log_received(
            command="listing.upsert",
            tenant=self.tenant,
            idempotency_key="",
            actor=None,
            trace_id=None,
            origin="",
            payload=None,
        )

        stored = CommandLog.objects.get(pk=row.pk)
        self.assertIsNone(stored.idempotency_key)  # "" -> NULL
        self.assertEqual(stored.actor, {})  # None -> {}
        self.assertEqual(stored.trace_id, "")  # None -> ""
        self.assertEqual(stored.adapter_id, "")  # "" -> ""
        self.assertIsNone(stored.payload)  # None is stored as-is

    def test_a_null_tenant_is_accepted(self):
        # spec §11.2: a command that never resolved a tenant still leaves a row.
        row = log_received(
            command="operator.create",
            tenant=None,
            idempotency_key=None,
            actor=ACTOR,
            trace_id=None,
            origin="",
            payload={"email": "x@example.test"},
        )
        stored = CommandLog.objects.get(pk=row.pk)
        self.assertIsNone(stored.tenant_id)


class LogConcludeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def _received(self):
        return log_received(
            command="listing.upsert",
            tenant=self.tenant,
            idempotency_key="k1",
            actor=ACTOR,
            trace_id="",
            origin="",
            payload=PAYLOAD,
        )

    def test_applied_sets_outcome_and_event_id_only(self):
        row = self._received()
        received_at = row.received_at

        log_conclude(row, outcome="applied", result_event_id="evt_1")

        stored = CommandLog.objects.get(pk=row.pk)
        self.assertEqual(stored.outcome, "applied")
        self.assertEqual(stored.result_event_id, "evt_1")
        self.assertIsNone(stored.problem)
        self.assertIsNotNone(stored.concluded_at)
        # untouched by conclude
        self.assertEqual(stored.received_at, received_at)
        self.assertEqual(stored.payload, PAYLOAD)

    def test_rejected_records_the_problem_and_no_event_id(self):
        row = self._received()

        log_conclude(row, outcome="rejected", problem={"field": "status"})

        stored = CommandLog.objects.get(pk=row.pk)
        self.assertEqual(stored.outcome, "rejected")
        self.assertEqual(stored.problem, {"field": "status"})
        self.assertEqual(stored.result_event_id, "")
        self.assertIsNotNone(stored.concluded_at)

    def test_applied_with_no_event_id_stores_empty_string(self):
        row = self._received()

        log_conclude(row, outcome="applied")

        stored = CommandLog.objects.get(pk=row.pk)
        self.assertEqual(stored.outcome, "applied")
        self.assertEqual(stored.result_event_id, "")


class LogReplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def test_replay_row_is_written_already_concluded(self):
        prior = log_received(
            command="listing.upsert",
            tenant=self.tenant,
            idempotency_key="k1",
            actor=ACTOR,
            trace_id="",
            origin="",
            payload=PAYLOAD,
        )
        log_conclude(prior, outcome="applied", result_event_id="evt_prior")

        log_replay(
            command="listing.upsert",
            tenant=self.tenant,
            idempotency_key="k1",
            actor=ACTOR,
            trace_id="",
            prior=CommandLog.objects.get(pk=prior.pk),
        )

        replay = CommandLog.objects.get(problem={"idempotent_replay": True})
        self.assertEqual(replay.command, "listing.upsert")
        self.assertEqual(replay.tenant, self.tenant)
        self.assertEqual(replay.idempotency_key, "k1")
        self.assertEqual(replay.actor, ACTOR)
        self.assertEqual(replay.outcome, "applied")
        self.assertEqual(replay.result_event_id, "evt_prior")
        self.assertIsNone(replay.payload)
        self.assertIsNotNone(replay.received_at)
        self.assertIsNotNone(replay.concluded_at)
        # both stamped from the same now()
        self.assertEqual(replay.received_at, replay.concluded_at)


class AutocommitTests(TransactionTestCase):
    """No wrapping transaction: each write lands on its own."""

    def setUp(self):
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def _received(self, **over):
        kw = dict(
            command="listing.upsert",
            tenant=self.tenant,
            idempotency_key=None,
            actor=ACTOR,
            trace_id="",
            origin="",
            payload=PAYLOAD,
        )
        kw.update(over)
        return log_received(**kw)

    def test_log_received_writes_with_no_open_transaction(self):
        self.assertFalse(transaction.get_connection().in_atomic_block)

        row = self._received()

        self.assertTrue(CommandLog.objects.filter(pk=row.pk).exists())

    def test_received_row_survives_a_rollback_of_a_later_transaction(self):
        row = self._received(idempotency_key="survives")

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self._received(idempotency_key="rolled-back")
                raise RuntimeError("boom")

        self.assertTrue(
            CommandLog.objects.filter(idempotency_key="survives").exists()
        )
        self.assertFalse(
            CommandLog.objects.filter(idempotency_key="rolled-back").exists()
        )

    def test_require_autocommit_passes_outside_and_raises_inside(self):
        self.assertIsNone(require_autocommit())

        with transaction.atomic():
            with self.assertRaises(MustNotBeInTransaction):
                require_autocommit()
