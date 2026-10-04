"""The drain's adapter contract (invariant 8, #238; decisions.md §4.12): what a
subscriber is handed, which tenants it is offered events for, and how its retry
hints are honoured.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TransactionTestCase
from django.utils import timezone

from audit import events
from audit.models import OutboxDelivery
from audit.outbox import emit
from audit.worker.drain import BACKOFF_CAP_SECONDS, drain_once, fan_out_once
from directory.models import ListingType
from osds.adapters import Result, override_subscribers, subscribers_for
from tenants.models import Tenant

LEAD = {
    "lead": {
        "id": "lead_1", "kind": "contact_form", "name": "Priya R.",
        "email": "priya@example.test", "phone_e164": "+13125550188",
        "message": "Burst pipe, need someone today.", "spam_score": 0.0,
    },
    "listing_id": "listing_1",
    "source_page": "/p/x",
    "consent": {"contact_by_business": {"granted": True, "ip": "198.51.100.7", "text_version": "lead-consent-v1"}},
}


class Spy:
    """Records what it is handed."""

    def __init__(self, id="spy", *, scopes=None, subscribes=None, wants=None, result=None):
        self.id = id
        if scopes is not None:
            self.scopes = scopes
        if subscribes is not None:
            self.subscribes = subscribes
        if wants is not None:
            self.wants = wants
        self.got = []
        self.result = result or Result.ok()

    def handle(self, envelope, ctx):
        self.got.append((envelope, ctx))
        return self.result(envelope) if callable(self.result) else self.result


class _Base(TransactionTestCase):
    def setUp(self):
        self.subs = self.enterContext(override_subscribers())
        self.tenant = Tenant.objects.create(slug="acme", name="Acme", primary_domain="acme.test")

    def configure(self, adapter_id="spy", **config):
        self.tenant.settings = {"adapters": {adapter_id: config}}
        self.tenant.save(update_fields=["settings"])
        self.tenant.refresh_from_db()

    def lead(self):
        return emit(events.LEAD_CAPTURED, subject="lead_1", tenant=self.tenant, data=LEAD)

    def drain(self, minutes=0):
        return drain_once(now=timezone.now() + timedelta(minutes=minutes))


class RedactionAtTheDrainTests(_Base):
    def test_a_subscriber_that_asks_for_nothing_never_sees_personal_data(self):
        spy = Spy()
        self.subs.append(spy)
        self.configure(scope_pii_contact=True, scope_pii_message=True)  # the tenant is generous; the adapter did not ask
        self.lead()
        self.drain()
        [(envelope, _)] = spy.got
        lead = envelope["data"]["lead"]
        self.assertEqual((lead["name"], lead["email"], lead["phone_e164"], lead["message"]), (None, None, None, None))
        self.assertIsNone(envelope["data"]["consent"]["contact_by_business"]["ip"])
        self.assertIn("lead.email", envelope["redacted"])

    def test_asking_is_not_receiving(self):
        spy = Spy(scopes=frozenset({"pii:contact", "pii:message"}))
        self.subs.append(spy)
        self.lead()
        self.drain()  # the tenant has granted nothing
        lead = spy.got[0][0]["data"]["lead"]
        self.assertEqual((lead["email"], lead["message"]), (None, None))

    def test_a_granted_scope_releases_exactly_that_much(self):
        spy = Spy(scopes=frozenset({"pii:contact", "pii:message"}))
        self.subs.append(spy)
        self.configure(scope_pii_contact=True)
        self.lead()
        self.drain()
        lead = spy.got[0][0]["data"]["lead"]
        self.assertEqual((lead["email"], lead["name"]), ("priya@example.test", "Priya R."))
        self.assertIsNone(lead["message"])
        self.assertEqual(spy.got[0][0]["redacted"], ["lead.message"])

    def test_both_grants_release_everything(self):
        spy = Spy(scopes=frozenset({"pii:contact", "pii:message"}))
        self.subs.append(spy)
        self.configure(scope_pii_contact=True, scope_pii_message=True)
        self.lead()
        self.drain()
        envelope = spy.got[0][0]
        self.assertEqual(envelope["data"], LEAD)
        self.assertEqual(envelope["redacted"], [])

    def test_the_grant_is_per_adapter(self):
        a, b = Spy("a", scopes=frozenset({"pii:contact"})), Spy("b", scopes=frozenset({"pii:contact"}))
        self.subs.extend([a, b])
        self.tenant.settings = {"adapters": {"a": {"scope_pii_contact": True}}}
        self.tenant.save(update_fields=["settings"])
        self.lead()
        self.drain()
        self.assertEqual(a.got[0][0]["data"]["lead"]["email"], "priya@example.test")
        self.assertIsNone(b.got[0][0]["data"]["lead"]["email"])

    def test_the_stored_event_is_untouched(self):
        self.subs.append(Spy())
        event = self.lead()
        self.drain()
        event.refresh_from_db()
        self.assertEqual(event.data["lead"]["email"], "priya@example.test")

    def test_a_tenant_event_reaches_a_subscriber_redacted_and_without_a_context(self):
        spy = Spy(scopes=frozenset({"pii:contact"}))
        self.subs.append(spy)
        emit(events.TENANT_SETTINGS_CHANGED, subject=self.tenant.public_id, tenant=self.tenant,
             data={"changed_by": "op", "changes": [{"op": "add", "path": "/smtp", "value": {"host": "mail.x"}}]})
        self.drain()
        envelope, ctx = spy.got[0]
        self.assertIsNone(ctx)
        self.assertIsNone(envelope["data"]["changes"][0]["value"])
        self.assertNotIn("tenant", envelope)

    def test_an_event_nobody_classified_is_withheld_in_full(self):
        spy = Spy(scopes=frozenset({"pii:contact"}))
        self.subs.append(spy)
        self.configure(scope_pii_contact=True)
        emit(events.REVIEW_SUBMITTED, subject="rev_1", tenant=self.tenant, data={"body": "great"})
        self.drain()
        self.assertEqual(spy.got[0][0]["data"], {})
        self.assertEqual(spy.got[0][0]["redacted"], ["data"])

    def test_private_custom_fields_need_the_contact_scope(self):
        ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B", label_plural="Bs", path_segment="bs",
            fields=[
                {"key": "hours", "label": "Hours", "type": "text", "public": True},
                {"key": "license", "label": "License", "type": "text", "public": False},
            ],
        )
        spy = Spy(scopes=frozenset({"pii:contact"}))
        self.subs.append(spy)
        data = {"type": "business", "name": "H", "contact": {"email": "o@x.test"}, "custom_fields": {"hours": "9-5", "license": "LIC-9"}}
        emit(events.LISTING_CREATED, subject="listing_1", tenant=self.tenant, data=data)
        self.drain()
        fields = spy.got[0][0]["data"]["custom_fields"]
        self.assertEqual(fields, {"hours": "9-5", "license": None})

        self.configure(scope_pii_contact=True)
        spy.got.clear()
        emit(events.LISTING_CREATED, subject="listing_2", tenant=self.tenant, data=data)
        self.drain()
        self.assertEqual(spy.got[0][0]["data"]["custom_fields"], {"hours": "9-5", "license": "LIC-9"})
        self.assertEqual(spy.got[0][0]["data"]["contact"], {"email": "o@x.test"})


class ContextTests(_Base):
    def test_a_subscriber_gets_a_context_for_the_deliverys_tenant(self):
        spy = Spy()
        self.subs.append(spy)
        self.configure(flag=True)
        self.lead()
        self.drain()
        ctx = spy.got[0][1]
        self.assertEqual((ctx.tenant_id, ctx.tenant_slug, ctx.config), (self.tenant.public_id, "acme", {"flag": True}))
        self.assertIsNotNone(ctx.logger)

    def test_the_handler_gets_the_envelope_tenant_block(self):
        spy = Spy()
        self.subs.append(spy)
        self.lead()
        self.drain()
        self.assertEqual(spy.got[0][0]["tenant"]["slug"], "acme")


class FanOutTests(_Base):
    def deliveries(self):
        return OutboxDelivery.all_tenants.count()

    def test_subscribes_patterns_choose_the_events(self):
        spy = Spy(subscribes=("lead.*",))
        self.subs.append(spy)
        self.lead()
        emit(events.CLAIM_DISPUTED, subject="claim_1", tenant=self.tenant, data={"claim": {"id": "c"}})
        self.drain()
        self.assertEqual([e["type"] for e, _ in spy.got], ["lead.captured"])

    def test_subscribers_for_matches_globs(self):
        self.subs.extend([Spy("a", subscribes=("claim.*",)), Spy("b", subscribes=("lead.captured", "billing.*")), Spy("c")])
        self.assertEqual({s.id for s in subscribers_for("claim.submitted")}, {"a", "c"})
        self.assertEqual({s.id for s in subscribers_for("lead.captured")}, {"b", "c"})
        self.assertEqual({s.id for s in subscribers_for("lead.marked_spam")}, {"c"})

    def test_a_filter_that_says_no_creates_no_delivery_row_but_the_event_is_dispatched(self):
        seen = []
        self.subs.append(Spy(wants=lambda event_type, config: seen.append((event_type, config)) or False))
        event = self.lead()
        fan_out_once(now=timezone.now())
        self.assertEqual(self.deliveries(), 0)
        event.refresh_from_db()
        self.assertEqual(event.status, "dispatched")
        self.assertEqual(seen, [("lead.captured", {})])

    def test_the_filter_gets_the_adapters_config_never_the_tenant(self):
        seen = []
        self.subs.append(Spy(wants=lambda event_type, config: seen.append(config) or True))
        self.configure(enabled=True, events="*")
        self.lead()
        fan_out_once(now=timezone.now())
        self.assertEqual(seen, [{"enabled": True, "events": "*"}])

    def test_a_tenant_event_is_offered_with_no_config(self):
        seen = []
        self.subs.append(Spy(wants=lambda event_type, config: seen.append(config) or False))
        emit(events.TENANT_CREATED, subject=self.tenant.public_id, tenant=self.tenant, data={"slug": "acme", "mode": "single", "created_by": "op"})
        fan_out_once(now=timezone.now())
        self.assertEqual(seen, [None])

    def test_a_filter_that_raises_does_not_drop_the_event(self):
        def boom(event_type, config):
            raise RuntimeError("bug in the adapter")

        spy = Spy(wants=boom)
        self.subs.append(spy)
        self.lead()
        with self.assertLogs("osds.drain", "WARNING") as logs:
            self.drain()
        self.assertEqual(len(spy.got), 1)
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("bug in the adapter", logs.output[0])

    def test_a_subscriber_with_no_filter_is_offered_everything(self):
        self.subs.append(Spy())
        self.lead()
        fan_out_once(now=timezone.now())
        self.assertEqual(self.deliveries(), 1)


class RetryHintTests(_Base):
    def retry_with(self, after_ms):
        spy = Spy(result=Result.retry(after_ms, "slow down"))
        self.subs.append(spy)
        self.lead()
        t0 = timezone.now()
        drain_once(now=t0)
        delivery = OutboxDelivery.all_tenants.get()
        return (delivery.next_attempt_at - t0).total_seconds(), delivery

    def test_a_longer_hint_wins(self):
        delay, delivery = self.retry_with(600_000)
        self.assertEqual(delay, 600)
        self.assertEqual(delivery.attempt, 1)

    def test_a_hint_beyond_the_cap_is_clamped_to_one_hour(self):
        delay, _ = self.retry_with(7_200_000)
        self.assertEqual(delay, BACKOFF_CAP_SECONDS)

    def test_a_shorter_hint_never_retries_sooner_than_the_schedule(self):
        # attempt 1's schedule is uniform(0, 1s); a 1ms hint changes nothing.
        delay, _ = self.retry_with(1)
        self.assertLessEqual(delay, 1)

    def test_no_hint_is_the_plain_schedule(self):
        delay, _ = self.retry_with(0)
        self.assertLessEqual(delay, 1)

    def test_a_failure_with_no_hint_is_the_plain_schedule(self):
        self.subs.append(Spy(result=Result.failed("http 503")))
        self.lead()
        t0 = timezone.now()
        drain_once(now=t0)
        delivery = OutboxDelivery.all_tenants.get()
        self.assertLessEqual((delivery.next_attempt_at - t0).total_seconds(), 1)
        self.assertEqual(delivery.last_error, "http 503")


class ExceptionTextTests(_Base):
    def test_only_the_exception_class_is_recorded(self):
        class Boom:
            id = "boom"

            def handle(self, envelope, ctx):
                raise ConnectionError("failed to reach https://hooks.example.test/T0K3N?x=priya@example.test")

        self.subs.append(Boom())
        self.lead()
        self.drain()
        delivery = OutboxDelivery.all_tenants.get()
        self.assertEqual(delivery.last_error, "ConnectionError")
        self.assertEqual(delivery.status, "pending")
