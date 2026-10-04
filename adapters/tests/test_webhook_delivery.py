"""The webhook adapter through the real drain (decisions.md §4.12): fan-out,
redaction, signing, retries, dead letters and replay, with a fake transport
under the egress layer so nothing touches the network.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from adapters.webhook import signing
from adapters.webhook.adapter import WebhookAdapter
from audit import deliveries, events
from audit.models import CommandLog, OutboxDelivery
from audit.outbox import emit
from audit.worker.drain import MAX_ATTEMPTS, drain_once
from osds.adapter_api import HttpResponse
from osds.adapter_context import build_context
from osds.adapters import override_subscribers
from tenants.models import Operator, StaffMembership, Tenant
from tenants.secrets import set_secret

URL = "https://hooks.example.test/in/T0K3N-abc123"
KEY = "whsec_" + "ab" * 32
PUBLIC = "93.184.216.34"

LEAD = {
    "lead": {
        "id": "lead_1", "kind": "contact_form", "name": "Priya R.", "email": "priya@example.test",
        "phone_e164": "+13125550188", "message": "Burst pipe, need someone today.", "spam_score": 0.0,
    },
    "listing_id": "listing_1",
    "source_page": "/p/x",
    "consent": {"contact_by_business": {"granted": True, "ip": "198.51.100.7", "text_version": "lead-consent-v1"}},
}


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.answers = []  # queued HttpResponse, then the default
        self.default = HttpResponse(200, b"ok")
        self.raises = None

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.answers.pop(0) if self.answers else self.default


class _Base(TransactionTestCase):
    def setUp(self):
        self.resolves = [PUBLIC]
        self.transport = FakeTransport()

        def build(tenant, provider, **kw):
            # Read at call time, so a test can change what the name resolves to.
            return build_context(tenant, provider, resolver=lambda host: list(self.resolves), transport=self.transport)

        patcher = mock.patch("audit.worker.drain.build_context", build)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.enterContext(override_subscribers(WebhookAdapter()))
        self.tenant = self.make_tenant("acme", KEY)

    def make_tenant(self, slug, key, **config):
        tenant = Tenant.objects.create(
            slug=slug, name=slug.title(), primary_domain=f"{slug}.test",
            settings={"adapters": {"webhook": {"enabled": True, "events": "*", "host": "hooks.example.test", **config}}},
        )
        set_secret("webhook_url", URL, tenant=tenant)
        set_secret("webhook_signing_secret", key, tenant=tenant)
        return tenant

    def lead(self, subject="lead_1", tenant=None):
        return emit(events.LEAD_CAPTURED, subject=subject, tenant=tenant or self.tenant, data=LEAD)

    def drain(self, minutes=0):
        return drain_once(now=timezone.now() + timedelta(minutes=minutes))

    def body(self, index=0):
        return json.loads(self.transport.calls[index]["body"])

    def delivery(self, event=None):
        qs = OutboxDelivery.all_tenants.filter(adapter_id="webhook")
        return qs.get(event=event) if event else qs.get()


class DeliveryTests(_Base):
    def test_an_event_is_posted_signed_and_marked_delivered(self):
        event = self.lead()
        self.drain()
        [call] = self.transport.calls
        self.assertEqual((call["method"], call["scheme"], call["host"], call["ip"]), ("POST", "https", "hooks.example.test", PUBLIC))
        self.assertEqual(call["target"], "/in/T0K3N-abc123")
        self.assertEqual(call["headers"]["X-OSDS-Event-Id"], event.event_id)
        self.assertTrue(signing.verify(
            KEY, call["headers"]["X-OSDS-Signature"], call["body"], now=int(timezone.now().timestamp()), tolerance=60
        ))
        self.assertEqual(self.delivery(event).status, "delivered")

    def test_personal_data_is_withheld_by_default(self):
        self.lead()
        self.drain()
        body = self.transport.calls[0]["body"].decode()
        for secret in ("priya@example.test", "Priya R.", "+13125550188", "Burst pipe", "198.51.100.7"):
            self.assertNotIn(secret, body)
        sent = self.body()
        self.assertIn("lead.email", sent["redacted"])
        self.assertEqual(sent["data"]["lead"]["id"], "lead_1")
        self.assertEqual(sent["tenant"]["slug"], "acme")

    def test_contact_alone_releases_contact_and_keeps_the_message_back(self):
        self.tenant.settings["adapters"]["webhook"]["scope_pii_contact"] = True
        self.tenant.save(update_fields=["settings"])
        self.lead()
        self.drain()
        lead = self.body()["data"]["lead"]
        self.assertEqual((lead["email"], lead["name"]), ("priya@example.test", "Priya R."))
        self.assertIsNone(lead["message"])

    def test_both_grants_release_everything(self):
        self.tenant.settings["adapters"]["webhook"].update(scope_pii_contact=True, scope_pii_message=True)
        self.tenant.save(update_fields=["settings"])
        self.lead()
        self.drain()
        sent = self.body()
        self.assertEqual(sent["data"], LEAD)
        self.assertEqual(sent["redacted"], [])

    def test_a_tenant_that_has_not_configured_it_gets_no_rows(self):
        other = Tenant.objects.create(slug="plain", name="Plain", primary_domain="plain.test")
        self.lead(tenant=other)
        self.drain()
        self.assertFalse(OutboxDelivery.all_tenants.filter(tenant=other).exists())
        self.assertEqual(self.transport.calls, [])

    def test_a_turned_off_endpoint_gets_no_rows(self):
        self.tenant.settings["adapters"]["webhook"]["enabled"] = False
        self.tenant.save(update_fields=["settings"])
        self.lead()
        self.drain()
        self.assertFalse(OutboxDelivery.all_tenants.exists())

    def test_event_filters_choose_what_is_sent(self):
        self.tenant.settings["adapters"]["webhook"]["events"] = "claim.*"
        self.tenant.save(update_fields=["settings"])
        self.lead()
        emit(events.CLAIM_DISPUTED, subject="claim_1", tenant=self.tenant, data={"claim": {"id": "c"}})
        self.drain()
        self.assertEqual([self.body(i)["type"] for i in range(len(self.transport.calls))], ["claim.disputed"])

    def test_tenant_events_are_never_sent(self):
        emit(events.TENANT_CREATED, subject=self.tenant.public_id, tenant=self.tenant,
             data={"slug": "acme", "mode": "single", "created_by": "op"})
        self.drain()
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(OutboxDelivery.all_tenants.exists())

    def test_a_disabled_endpoint_after_fan_out_skips_instead_of_failing(self):
        event = self.lead()
        from audit.worker.drain import fan_out_once

        fan_out_once(now=timezone.now())
        self.tenant.settings["adapters"]["webhook"]["enabled"] = False
        self.tenant.save(update_fields=["settings"])
        self.drain()
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.delivery(event).status, "delivered")  # skipped counts as done

    def test_each_tenant_signs_with_its_own_key_and_posts_its_own_events(self):
        other_key = "whsec_" + "ef" * 32
        other = self.make_tenant("beta", other_key)
        self.lead(subject="lead_a")
        self.lead(subject="lead_b", tenant=other)
        self.drain()
        by_tenant = {self.body(i)["tenant"]["slug"]: self.transport.calls[i] for i in range(len(self.transport.calls))}
        now = int(timezone.now().timestamp())
        self.assertTrue(signing.verify(KEY, by_tenant["acme"]["headers"]["X-OSDS-Signature"], by_tenant["acme"]["body"], now=now, tolerance=60))
        self.assertTrue(signing.verify(other_key, by_tenant["beta"]["headers"]["X-OSDS-Signature"], by_tenant["beta"]["body"], now=now, tolerance=60))
        self.assertFalse(signing.verify(KEY, by_tenant["beta"]["headers"]["X-OSDS-Signature"], by_tenant["beta"]["body"], now=now, tolerance=60))

    def test_private_custom_fields_follow_the_contact_grant(self):
        from directory.models import ListingType

        ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B", label_plural="Bs", path_segment="bs",
            fields=[{"key": "hours", "label": "H", "type": "text", "public": True},
                    {"key": "license", "label": "L", "type": "text", "public": False}],
        )
        emit(events.LISTING_CREATED, subject="listing_1", tenant=self.tenant,
             data={"type": "business", "contact": {"email": "o@x.test"}, "custom_fields": {"hours": "9-5", "license": "LIC-9"}})
        self.drain()
        sent = self.body()["data"]
        self.assertEqual(sent["custom_fields"], {"hours": "9-5", "license": None})
        self.assertIsNone(sent["contact"])


class RetryTests(_Base):
    def test_a_server_error_retries_then_gives_up_after_twelve_attempts(self):
        self.transport.default = HttpResponse(503, b"down")
        event = self.lead()
        for i in range(MAX_ATTEMPTS):
            self.drain(minutes=(i + 1) * 120)
        d = self.delivery(event)
        self.assertEqual((d.status, d.attempt, d.last_error), ("dead", MAX_ATTEMPTS, "http 503"))
        self.assertEqual(len(self.transport.calls), MAX_ATTEMPTS)

    def test_every_attempt_carries_the_same_event_id_for_dedupe(self):
        self.transport.default = HttpResponse(500, b"")
        event = self.lead()
        for i in range(3):
            self.drain(minutes=(i + 1) * 120)
        ids = {c["headers"]["X-OSDS-Event-Id"] for c in self.transport.calls}
        self.assertEqual(ids, {event.event_id})

    def test_410_dead_letters_at_once(self):
        self.transport.answers = [HttpResponse(410, b"gone")]
        event = self.lead()
        self.drain()
        d = self.delivery(event)
        self.assertEqual((d.status, d.attempt, d.last_error), ("dead", 1, "http 410"))

    def test_429_honours_retry_after(self):
        self.transport.answers = [HttpResponse(429, b"", {"retry-after": "600"})]
        self.lead()
        t0 = timezone.now()
        drain_once(now=t0)
        d = self.delivery()
        self.assertEqual(d.status, "pending")
        self.assertGreaterEqual((d.next_attempt_at - t0).total_seconds(), 600)
        self.assertLessEqual((d.next_attempt_at - t0).total_seconds(), 3600)

    def test_a_retry_after_beyond_an_hour_is_clamped(self):
        self.transport.answers = [HttpResponse(429, b"", {"retry-after": "86400"})]
        self.lead()
        t0 = timezone.now()
        drain_once(now=t0)
        self.assertEqual((self.delivery().next_attempt_at - t0).total_seconds(), 3600)

    def test_409_counts_as_delivered(self):
        self.transport.answers = [HttpResponse(409, b"")]
        self.lead()
        self.drain()
        self.assertEqual(self.delivery().status, "delivered")

    def test_a_redirect_is_not_followed_and_is_retried(self):
        self.transport.answers = [HttpResponse(302, b"", {"location": "https://evil.example.test/"})]
        self.lead()
        self.drain()
        d = self.delivery()
        self.assertEqual((d.status, d.attempt), ("pending", 1))
        self.assertEqual(len(self.transport.calls), 1)  # nothing was fetched from the Location

    def test_a_later_event_for_the_same_subject_waits_behind_a_failing_one(self):
        self.transport.answers = [HttpResponse(503, b"")]
        first = self.lead("lead_1")
        second = self.lead("lead_1")
        self.drain()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.delivery(second).attempt, 0)
        self.drain(minutes=120)
        self.drain(minutes=240)
        self.assertEqual(self.delivery(first).status, "delivered")
        self.assertEqual(self.delivery(second).status, "delivered")
        order = [c["headers"]["X-OSDS-Event-Id"] for c in self.transport.calls]
        self.assertEqual(order[1:], [first.event_id, second.event_id])

    def test_the_error_recorded_is_a_status_or_a_class_never_a_body_or_url(self):
        self.transport.answers = [HttpResponse(500, b"echo https://hooks.example.test/in/T0K3N priya@example.test")]
        self.lead()
        self.drain()
        self.assertEqual(self.delivery().last_error, "http 500")

    def test_a_transport_failure_is_retried_with_only_the_class_recorded(self):
        self.transport.raises = ConnectionResetError("reset talking to https://hooks.example.test/in/T0K3N")
        self.lead()
        self.drain()
        d = self.delivery()
        self.assertEqual((d.status, d.last_error), ("pending", "ConnectionResetError"))


class EgressTests(_Base):
    def test_a_private_destination_is_dead_lettered_at_once(self):
        self.resolves = ["10.0.0.5"]
        self.lead()
        self.drain()
        d = self.delivery()
        self.assertEqual((d.status, d.attempt, d.last_error), ("dead", 1, "EgressDenied"))
        self.assertEqual(self.transport.calls, [])

    def test_the_deployment_opt_in_lets_a_private_destination_through(self):
        self.resolves = ["10.0.0.5"]
        self.lead()
        with override_settings(OSDS_WEBHOOK_ALLOW_PRIVATE=True):
            self.drain()
        self.assertEqual(self.delivery().status, "delivered")
        self.assertEqual(self.transport.calls[0]["ip"], "10.0.0.5")

    def test_the_metadata_address_is_unreachable_even_with_the_opt_in(self):
        self.resolves = ["169.254.169.254"]
        self.lead()
        with override_settings(OSDS_WEBHOOK_ALLOW_PRIVATE=True):
            self.drain()
        self.assertEqual(self.delivery().last_error, "EgressDenied")
        self.assertEqual(self.transport.calls, [])

    def test_the_url_may_only_name_the_configured_host(self):
        set_secret("webhook_url", "https://elsewhere.example.test/x", tenant=self.tenant)
        self.lead()
        self.drain()
        self.assertEqual(self.delivery().last_error, "EgressDenied")
        self.assertEqual(self.transport.calls, [])


class ReplayTests(_Base):
    def admin(self, role=StaffMembership.Role.ADMIN):
        operator = Operator.objects.create_user(email=f"op{role}@acme.test", password="pw")
        StaffMembership.objects.create(
            operator=operator, tenant=self.tenant, role=role, status=StaffMembership.Status.ACTIVE
        )
        return operator

    def kill(self):
        self.transport.answers = [HttpResponse(410, b"")]
        event = self.lead()
        self.drain()
        self.assertEqual(self.delivery(event).status, "dead")
        return event

    def test_a_dead_letter_can_be_replayed_and_is_then_delivered(self):
        event = self.kill()
        deliveries.replay_delivery(self.tenant, adapter_id="webhook", event_id=event.event_id, operator=self.admin())
        d = self.delivery(event)
        self.assertEqual((d.status, d.attempt, d.last_error), ("pending", 0, ""))
        self.drain()
        self.assertEqual(self.delivery(event).status, "delivered")
        self.assertEqual(self.body(1)["id"], event.event_id)
        row = CommandLog.objects.get(command="delivery.replay")
        self.assertEqual((row.outcome, row.payload), ("applied", {"adapter_id": "webhook", "event_id": event.event_id}))

    def test_only_a_dead_delivery_can_be_replayed(self):
        event = self.lead()
        self.drain()
        with self.assertRaises(deliveries.ReplayRefused) as cm:
            deliveries.replay_delivery(self.tenant, adapter_id="webhook", event_id=event.event_id, operator=self.admin())
        self.assertEqual(cm.exception.reason, "not_dead")
        self.assertEqual(CommandLog.objects.get(command="delivery.replay").outcome, "rejected")

    def test_a_nulled_payload_is_not_replayed(self):
        event = self.kill()
        type(event).all_tenants.filter(pk=event.pk).update(data={}, payload_nulled_at=timezone.now())
        with self.assertRaises(deliveries.ReplayRefused) as cm:
            deliveries.replay_delivery(self.tenant, adapter_id="webhook", event_id=event.event_id, operator=self.admin())
        self.assertEqual(cm.exception.reason, "payload_nulled")
        self.assertEqual(self.delivery(event).status, "dead")

    def test_below_admin_is_blocked_and_logged(self):
        event = self.kill()
        with self.assertRaises(deliveries.ReplayRefused) as cm:
            deliveries.replay_delivery(
                self.tenant, adapter_id="webhook", event_id=event.event_id,
                operator=self.admin(StaffMembership.Role.MANAGER),
            )
        self.assertEqual(cm.exception.reason, "forbidden")
        self.assertEqual(CommandLog.objects.get(command="delivery.replay").outcome, "blocked")
        self.assertEqual(self.delivery(event).status, "dead")

    def test_another_tenants_delivery_is_not_found(self):
        event = self.kill()
        other = Tenant.objects.create(slug="beta", name="Beta")
        operator = Operator.objects.create_user(email="beta@x.test", password="pw")
        StaffMembership.objects.create(operator=operator, tenant=other, role=StaffMembership.Role.ADMIN,
                                       status=StaffMembership.Status.ACTIVE)
        with self.assertRaises(deliveries.ReplayRefused) as cm:
            deliveries.replay_delivery(other, adapter_id="webhook", event_id=event.event_id, operator=operator)
        self.assertEqual(cm.exception.reason, "not_found")

    def test_replay_keeps_order_a_later_event_waits_behind_the_replayed_one(self):
        self.transport.answers = [HttpResponse(410, b"")]
        first = self.lead("lead_1")
        self.drain()
        second = self.lead("lead_1")
        deliveries.replay_delivery(self.tenant, adapter_id="webhook", event_id=first.event_id, operator=self.admin())
        self.drain()
        self.drain(minutes=1)
        order = [c["headers"]["X-OSDS-Event-Id"] for c in self.transport.calls]
        self.assertEqual(order[1:], [first.event_id, second.event_id])


class HealthTests(_Base):
    def test_health_counts(self):
        self.transport.answers = [HttpResponse(200, b""), HttpResponse(503, b"")]
        self.lead("a")
        self.lead("b")
        self.drain()
        h = deliveries.health(self.tenant, "webhook")
        self.assertEqual((h.delivered, h.retrying, h.dead, h.pending, h.failing), (1, 1, 0, 0, True))
        self.assertEqual(h.last_error, "http 503")
        self.assertIsNotNone(h.last_delivered_at)

    def test_no_deliveries_is_healthy(self):
        h = deliveries.health(self.tenant, "webhook")
        self.assertEqual((h.failing, h.last_error, h.delivered), (False, "", 0))

    def test_health_is_per_tenant_and_per_adapter(self):
        self.transport.default = HttpResponse(410, b"")
        self.lead()
        self.drain()
        other = Tenant.objects.create(slug="beta", name="Beta")
        self.assertEqual(deliveries.health(other, "webhook").dead, 0)
        self.assertEqual(deliveries.health(self.tenant, "someone-else").dead, 0)
        self.assertEqual(deliveries.health(self.tenant, "webhook").dead, 1)

    def test_recent_lists_newest_first_and_filters_by_status(self):
        self.transport.answers = [HttpResponse(410, b"")]
        self.lead("a")
        self.lead("b")
        self.drain()
        self.assertEqual([d.status for d in deliveries.recent(self.tenant, "webhook")], ["delivered", "dead"])
        self.assertEqual([d.status for d in deliveries.recent(self.tenant, "webhook", status="dead")], ["dead"])
