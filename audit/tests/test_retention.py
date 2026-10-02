"""90-day retention sweeps (spec §11.2, #213): boundary, idempotence, scope.

Each job is driven directly with an explicit ``now``. No clock is read here
either: every age is built from one ``NOW``.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from audit import models as audit_models
from audit.envelope import to_wire
from audit.models import (
    AccessLog,
    CommandLog,
    OutboundMessage,
    OutboxDelivery,
    OutboxEvent,
)
from audit.worker import retention
from tenants.models import Tenant

NOW = timezone.now()
DAY = timedelta(days=1)
OLD = NOW - 91 * DAY
EXACTLY_90 = NOW - 90 * DAY
RECENT = NOW - 10 * DAY


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")
        cls.other = Tenant.objects.create(slug="other", name="Other")


class OutboundAddressTests(_Base):
    def _msg(self, *, age=OLD, status="sent", tenant=None, to="a@example.com"):
        return OutboundMessage.all_tenants.create(
            tenant=tenant or self.tenant,
            kind="claim.verification_code",
            to_address=to,
            subject="Your code",
            status=status,
            created_at=age,
            expires_at=age + timedelta(minutes=15),
        )

    def test_nulls_the_address_of_an_old_terminal_message(self):
        sent, dead, expired = (
            self._msg(status="sent"),
            self._msg(status="dead"),
            self._msg(status="expired"),
        )
        result = retention.null_outbound_addresses(now=NOW)
        self.assertEqual(result.done, 3)
        for msg in (sent, dead, expired):
            msg.refresh_from_db()
            self.assertIsNone(msg.to_address)

    def test_only_the_address_changes(self):
        msg = self._msg()
        retention.null_outbound_addresses(now=NOW)
        after = OutboundMessage.all_tenants.get(pk=msg.pk)
        self.assertEqual(
            (after.subject, after.kind, after.status, after.created_at),
            (msg.subject, msg.kind, msg.status, msg.created_at),
        )

    def test_the_boundary_is_strictly_older_than_90_days(self):
        boundary = self._msg(age=EXACTLY_90)
        just_over = self._msg(age=EXACTLY_90 - timedelta(seconds=1))
        retention.null_outbound_addresses(now=NOW)
        boundary.refresh_from_db()
        just_over.refresh_from_db()
        self.assertEqual(boundary.to_address, "a@example.com")
        self.assertIsNone(just_over.to_address)

    def test_a_recent_message_is_untouched(self):
        msg = self._msg(age=RECENT)
        retention.null_outbound_addresses(now=NOW)
        msg.refresh_from_db()
        self.assertEqual(msg.to_address, "a@example.com")

    def test_a_pending_message_is_never_nulled(self):
        # The drain would otherwise find a message it is still going to send
        # with nowhere to send it.
        msg = self._msg(status="pending")
        result = retention.null_outbound_addresses(now=NOW)
        msg.refresh_from_db()
        self.assertEqual((result.done, msg.to_address), (0, "a@example.com"))

    def test_spans_tenants(self):
        a, b = self._msg(), self._msg(tenant=self.other)
        retention.null_outbound_addresses(now=NOW)
        for msg in (a, b):
            msg.refresh_from_db()
            self.assertIsNone(msg.to_address)

    def test_is_idempotent(self):
        self._msg()
        self.assertEqual(retention.null_outbound_addresses(now=NOW).done, 1)
        again = retention.null_outbound_addresses(now=NOW)
        self.assertEqual((again.done, again.more), (0, False))

    def test_is_bounded_and_reports_backlog(self):
        for _ in range(5):
            self._msg()
        with mock.patch.object(retention, "RETENTION_CHUNK", 2):
            first = retention.null_outbound_addresses(now=NOW)
            second = retention.null_outbound_addresses(now=NOW)
            third = retention.null_outbound_addresses(now=NOW)
        self.assertEqual(
            [(r.done, r.more) for r in (first, second, third)],
            [(2, True), (2, True), (1, False)],
        )
        self.assertFalse(
            OutboundMessage.all_tenants.filter(to_address__isnull=False).exists()
        )


class EventPayloadTests(_Base):
    def _event(self, *, age=OLD, status="delivered", tenant=None, data=None):
        return OutboxEvent.all_tenants.create(
            type="listing.claimed",
            subject="listing_01",
            tenant=tenant or self.tenant,
            data={"email": "owner@example.com"} if data is None else data,
            status=status,
            created_at=age,
            occurred_at=age,
        )

    def _delivery(self, event, *, status):
        return OutboxDelivery.all_tenants.create(
            event=event,
            tenant=event.tenant,
            subject=event.subject,
            adapter_id="webhook",
            status=status,
        )

    def test_nulls_an_old_payload_and_stamps_it(self):
        event = self._event()
        result = retention.null_event_payloads(now=NOW)
        event.refresh_from_db()
        self.assertEqual(result.done, 1)
        self.assertEqual(event.data, {})
        self.assertEqual(event.payload_nulled_at, NOW)

    def test_the_envelope_survives_and_still_serialises(self):
        event = self._event()
        before = to_wire(event)
        retention.null_event_payloads(now=NOW)
        event.refresh_from_db()
        after = to_wire(event)
        self.assertEqual(after["data"], {})
        for key in ("id", "type", "version", "subject"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(
            (event.type, event.subject, event.tenant_id, event.status),
            ("listing.claimed", "listing_01", self.tenant.pk, "delivered"),
        )

    def test_the_boundary_is_strictly_older_than_90_days(self):
        boundary = self._event(age=EXACTLY_90)
        just_over = self._event(age=EXACTLY_90 - timedelta(seconds=1))
        retention.null_event_payloads(now=NOW)
        boundary.refresh_from_db()
        just_over.refresh_from_db()
        self.assertEqual(boundary.data, {"email": "owner@example.com"})
        self.assertIsNone(boundary.payload_nulled_at)
        self.assertEqual(just_over.data, {})

    def test_a_recent_event_is_untouched(self):
        event = self._event(age=RECENT)
        retention.null_event_payloads(now=NOW)
        event.refresh_from_db()
        self.assertEqual(event.data, {"email": "owner@example.com"})

    def test_an_event_still_awaiting_fan_out_is_untouched(self):
        event = self._event(status="pending")
        self.assertEqual(retention.null_event_payloads(now=NOW).done, 0)
        event.refresh_from_db()
        self.assertEqual(event.data, {"email": "owner@example.com"})

    def test_an_event_with_a_pending_delivery_is_untouched(self):
        # A delivery still to be attempted would otherwise go out empty.
        event = self._event(status="dispatched")
        self._delivery(event, status="pending")
        self.assertEqual(retention.null_event_payloads(now=NOW).done, 0)
        event.refresh_from_db()
        self.assertEqual(event.data, {"email": "owner@example.com"})

    def test_a_dead_lettered_event_is_nulled_like_any_other(self):
        # decisions.md §4: dead letters are not exempt.
        event = self._event(status="dead")
        self._delivery(event, status="dead")
        retention.null_event_payloads(now=NOW)
        event.refresh_from_db()
        self.assertEqual(event.data, {})

    def test_an_event_whose_deliveries_all_settled_is_nulled(self):
        event = self._event(status="dispatched")
        self._delivery(event, status="delivered")
        retention.null_event_payloads(now=NOW)
        event.refresh_from_db()
        self.assertEqual(event.data, {})

    def test_spans_tenants(self):
        a, b = self._event(), self._event(tenant=self.other)
        self.assertEqual(retention.null_event_payloads(now=NOW).done, 2)
        for event in (a, b):
            event.refresh_from_db()
            self.assertEqual(event.data, {})

    def test_is_idempotent_and_keeps_the_first_stamp(self):
        event = self._event()
        retention.null_event_payloads(now=NOW)
        later = NOW + DAY
        again = retention.null_event_payloads(now=later)
        event.refresh_from_db()
        self.assertEqual((again.done, again.more), (0, False))
        self.assertEqual(event.payload_nulled_at, NOW)

    def test_is_bounded_and_reports_backlog(self):
        for _ in range(3):
            self._event()
        with mock.patch.object(retention, "RETENTION_CHUNK", 2):
            first = retention.null_event_payloads(now=NOW)
            second = retention.null_event_payloads(now=NOW)
        self.assertEqual([(first.done, first.more), (second.done, second.more)],
                         [(2, True), (1, False)])


class CommandPayloadTests(_Base):
    def _cmd(self, *, age=OLD, payload=None, outcome="applied", concluded=True):
        return CommandLog.objects.create(
            command="listing.upsert",
            tenant=self.tenant,
            payload={"phone": "+15555550100"} if payload is None else payload,
            received_at=age,
            outcome=outcome,
            concluded_at=age if concluded else None,
            problem={"code": "x"} if outcome == "rejected" else None,
        )

    def test_nulls_the_payload_and_nothing_else(self):
        cmd = self._cmd(outcome="rejected")
        retention.null_command_payloads(now=NOW)
        after = CommandLog.objects.get(pk=cmd.pk)
        self.assertIsNone(after.payload)
        self.assertEqual(
            (after.command, after.outcome, after.problem, after.concluded_at,
             after.received_at, after.tenant_id, after.trace_id),
            (cmd.command, cmd.outcome, cmd.problem, cmd.concluded_at,
             cmd.received_at, cmd.tenant_id, cmd.trace_id),
        )

    def test_a_command_that_threw_mid_apply_is_nulled_too(self):
        cmd = self._cmd(outcome=None, concluded=False)
        retention.null_command_payloads(now=NOW)
        self.assertIsNone(CommandLog.objects.get(pk=cmd.pk).payload)

    def test_the_boundary_is_strictly_older_than_90_days(self):
        boundary = self._cmd(age=EXACTLY_90)
        just_over = self._cmd(age=EXACTLY_90 - timedelta(seconds=1))
        retention.null_command_payloads(now=NOW)
        self.assertIsNotNone(CommandLog.objects.get(pk=boundary.pk).payload)
        self.assertIsNone(CommandLog.objects.get(pk=just_over.pk).payload)

    def test_a_recent_command_is_untouched(self):
        cmd = self._cmd(age=RECENT)
        retention.null_command_payloads(now=NOW)
        self.assertIsNotNone(CommandLog.objects.get(pk=cmd.pk).payload)

    def test_a_command_with_no_tenant_is_swept(self):
        cmd = CommandLog.objects.create(
            command="x", payload={"a": 1}, received_at=OLD, outcome="rejected",
            concluded_at=OLD,
        )
        retention.null_command_payloads(now=NOW)
        self.assertIsNone(CommandLog.objects.get(pk=cmd.pk).payload)

    def test_is_idempotent(self):
        self._cmd()
        self.assertEqual(retention.null_command_payloads(now=NOW).done, 1)
        again = retention.null_command_payloads(now=NOW)
        self.assertEqual((again.done, again.more), (0, False))

    def test_is_bounded_and_reports_backlog(self):
        for _ in range(3):
            self._cmd()
        with mock.patch.object(retention, "RETENTION_CHUNK", 2):
            first = retention.null_command_payloads(now=NOW)
            second = retention.null_command_payloads(now=NOW)
        self.assertEqual([(first.done, first.more), (second.done, second.more)],
                         [(2, True), (1, False)])


TWO_YEARS = timedelta(days=730)


class AccessLogTests(_Base):
    def _row(self, age, *, tenant=None, resource_id="lead_1"):
        return AccessLog.all_tenants.create(
            tenant=tenant or self.tenant,
            actor={"type": "staff", "id": "op_1"},
            action="viewed",
            resource_type="lead",
            resource_id=resource_id,
            occurred_at=NOW - age,
        )

    def test_deletes_rows_older_than_two_years_and_keeps_the_rest(self):
        old = self._row(TWO_YEARS + timedelta(seconds=1))
        edge = self._row(TWO_YEARS, resource_id="lead_2")
        recent = self._row(timedelta(days=30), tenant=self.other, resource_id="lead_3")

        result = retention.prune_access_log(now=NOW)

        self.assertEqual(result.done, 1)
        remaining = set(AccessLog.all_tenants.values_list("pk", flat=True))
        self.assertEqual(remaining, {edge.pk, recent.pk})
        self.assertNotIn(old.pk, remaining)

    def test_a_row_with_no_tenant_is_swept_too(self):
        row = AccessLog.all_tenants.create(
            tenant=None, actor={}, action="exported", resource_type="listing",
            occurred_at=NOW - TWO_YEARS - timedelta(days=1),
        )
        retention.prune_access_log(now=NOW)
        self.assertFalse(AccessLog.all_tenants.filter(pk=row.pk).exists())

    def test_is_idempotent(self):
        self._row(TWO_YEARS + timedelta(days=5))
        self.assertEqual(retention.prune_access_log(now=NOW).done, 1)
        again = retention.prune_access_log(now=NOW)
        self.assertEqual((again.done, again.more), (0, False))

    def test_is_bounded_and_reports_backlog(self):
        for i in range(3):
            self._row(TWO_YEARS + timedelta(days=5 + i), resource_id=f"lead_{i}")
        with mock.patch.object(retention, "RETENTION_CHUNK", 2):
            first = retention.prune_access_log(now=NOW)
            second = retention.prune_access_log(now=NOW)
        self.assertEqual(
            [(first.done, first.more), (second.done, second.more)], [(2, True), (1, False)]
        )

    def test_the_window_is_two_years(self):
        self.assertEqual(retention.ACCESS_LOG_KEEP, TWO_YEARS)


class RegistrationTests(TestCase):
    def test_the_retention_jobs_are_registered_daily(self):
        import io

        from audit.worker.jobs import RETENTION_EVERY, build_tick_registry

        jobs = {j.name: j for j in build_tick_registry(out=io.StringIO()).jobs}
        for name in (
            "import_pre_image_retention",
            "outbound_address_retention",
            "event_payload_retention",
            "command_payload_retention",
            "access_log_retention",
        ):
            self.assertEqual(jobs[name].every, RETENTION_EVERY, name)
        self.assertEqual(RETENTION_EVERY, timedelta(hours=24))

    def test_a_tick_runs_them_through_the_registry_with_the_passes_clock(self):
        import io

        from audit.worker.jobs import build_tick_registry

        registry = build_tick_registry(out=io.StringIO())
        with mock.patch.object(
            audit_models.OutboundMessage.all_tenants, "filter",
            wraps=audit_models.OutboundMessage.all_tenants.filter,
        ) as spy:
            stats = registry.run_due(now=NOW)
        self.assertEqual(stats.failures, [])
        self.assertTrue(spy.called)
