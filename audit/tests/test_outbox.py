"""Worker PR 1: the delivery row, the on-commit NOTIFY, the wire envelope, and
the (empty) adapter registry seam.
"""

from __future__ import annotations

from unittest import mock

from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from audit.envelope import to_wire
from audit.models import OutboxDelivery, OutboxEvent
from audit.outbox import emit
from osds.adapters import Result, subscribers_for
from tenants.models import Tenant


class OutboxDeliveryConstraintTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")
        cls.event = OutboxEvent.all_tenants.create(
            type="listing.created", subject="listing_x", tenant=cls.tenant
        )
        cls.event2 = OutboxEvent.all_tenants.create(
            type="listing.updated", subject="listing_x", tenant=cls.tenant
        )

    def _delivery(self, **over):
        # all_tenants: the drain writes and scans delivery rows with no tenant
        # in ambient scope, exactly like this test.
        kw = dict(
            event=self.event,
            tenant=self.tenant,
            subject="listing_x",
            adapter_id="webhook",
        )
        kw.update(over)
        return OutboxDelivery.all_tenants.create(**kw)

    def test_one_delivery_per_event_and_adapter(self):
        self._delivery()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._delivery()

    def test_same_event_other_adapter_is_allowed(self):
        self._delivery(adapter_id="a")
        self._delivery(adapter_id="b")
        self.assertEqual(
            OutboxDelivery.all_tenants.filter(event=self.event).count(), 2
        )

    def test_other_event_same_adapter_is_allowed(self):
        self._delivery(event=self.event)
        self._delivery(event=self.event2)
        self.assertEqual(OutboxDelivery.all_tenants.count(), 2)


class NotifyOnCommitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def test_notify_fires_after_commit(self):
        with mock.patch("audit.outbox._notify_outbox") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                emit("listing.created", subject="listing_x", tenant=self.tenant)
        notify.assert_called_once()

    def test_notify_not_sent_on_rollback(self):
        with mock.patch("audit.outbox._notify_outbox") as notify:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    emit(
                        "listing.created",
                        subject="listing_x",
                        tenant=self.tenant,
                    )
                    raise RuntimeError("boom")
        notify.assert_not_called()


class ToWireTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(
            slug="chicago-plumbers",
            name="Chicago Plumbers",
            primary_domain="chicagoplumbers.example",
        )

    def test_normal_event_carries_the_tenant_block(self):
        ev = OutboxEvent.all_tenants.create(
            type="claim.approved",
            subject="listing_abc",
            tenant=self.tenant,
            actor={"type": "owner", "id": "usr_1"},
            data={"k": "v"},
        )
        wire = to_wire(ev)

        self.assertEqual(wire["id"], ev.event_id)
        self.assertEqual(wire["type"], "claim.approved")
        self.assertEqual(wire["version"], 1)
        self.assertEqual(wire["subject"], "listing_abc")
        self.assertEqual(wire["actor"], {"type": "owner", "id": "usr_1"})
        self.assertEqual(wire["data"], {"k": "v"})
        self.assertIsNone(wire["origin"])
        self.assertEqual(wire["trace_id"], ev.event_id)  # empty -> id
        self.assertRegex(
            wire["occurred_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
        )
        self.assertEqual(
            wire["tenant"],
            {
                "id": self.tenant.public_id,
                "slug": "chicago-plumbers",
                "domain": "chicagoplumbers.example",
            },
        )

    def test_tenant_event_omits_the_tenant_block(self):
        ev = OutboxEvent.all_tenants.create(
            type="tenant.created",
            subject=self.tenant.public_id,
            tenant=self.tenant,
            data={"slug": "chicago-plumbers"},
        )
        wire = to_wire(ev)

        self.assertNotIn("tenant", wire)
        self.assertEqual(wire["type"], "tenant.created")
        self.assertEqual(wire["subject"], self.tenant.public_id)
        self.assertEqual(wire["data"], {"slug": "chicago-plumbers"})


class RegistrySeamTests(SimpleTestCase):
    def test_subscribers_for_is_empty(self):
        for event_type in ("listing.created", "tenant.created", "any.thing"):
            self.assertEqual(subscribers_for(event_type), [])
        self.assertIsInstance(subscribers_for("x"), list)

    def test_result_constructors(self):
        self.assertEqual(Result.ok().status, "ok")
        self.assertEqual(Result.skipped("x").status, "skipped")
        r = Result.retry(1500, "later")
        self.assertEqual((r.status, r.retry_after_ms), ("retry", 1500))
        f = Result.failed("nope", permanent=True)
        self.assertEqual((f.status, f.permanent), ("failed", True))
