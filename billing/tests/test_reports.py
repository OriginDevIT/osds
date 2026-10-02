"""``entitlement.reportPayment`` (decisions.md §4.11): a provider reports an
outcome, core decides what it means, applies it once, and emits every event."""

from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError

from audit.models import CommandLog, OutboxEvent
from billing import machine, reports
from billing.machine import DUNNING
from billing.models import CheckoutAttempt, Entitlement, PaymentReceipt
from billing.tests.base import DAY, T0
from billing.tests.payments_base import ADAPTER_ID, PaymentTestCase
from osds.adapter_api import PaymentReport
from osds.tenancy import tenant_context

S = Entitlement.Status
REF = {"adapter": ADAPTER_ID, "external_id": "sub_1"}


class _Base(PaymentTestCase):
    def attempt(self, tier=None, listing=None, **kw):
        return CheckoutAttempt.all_tenants.create(
            tenant=self.tenant, listing=listing or self.listing, tier=tier or self.featured,
            adapter_id=ADAPTER_ID, expires_at=T0 + DAY, **kw,
        )

    def report(self, kind, event_id, **kw):
        now = kw.pop("now", T0)
        report = PaymentReport(kind=kind, external_event_id=event_id, **kw)
        with tenant_context(self.tenant):
            return reports.report_payment(self.tenant, adapter_id=ADAPTER_ID, report=report, now=now)

    def subscribe(self, event_id="evt_start", **kw):
        attempt = self.attempt()
        self.report("checkout_completed", event_id, checkout_attempt_id=attempt.public_id,
                    period_end=T0 + 30 * DAY, payment_ref=REF, **kw)
        return attempt


class CheckoutCompletedTests(_Base):
    def test_a_completed_checkout_starts_the_entitlement(self):
        attempt = self.attempt()
        since = self.last_event_id()
        result = self.report("checkout_completed", "evt_1", checkout_attempt_id=attempt.public_id,
                             period_end=T0 + 30 * DAY, payment_ref=REF)
        self.assertEqual(result.outcome, "applied")
        ent = self.ent()
        self.assertEqual((ent.status, ent.tier, ent.payment_ref), (S.ACTIVE, self.featured, REF))
        self.assertEqual(self.fresh().current_tier, self.featured)
        types = [e.type for e in self.events(since=since)]
        self.assertEqual(types, ["entitlement.started", "listing.tier_changed", "billing.subscription_started"])
        started = self.events("billing.subscription_started").get().data
        self.assertEqual((started["subscription"], started["plan"]), ("sub_1", "featured"))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, CheckoutAttempt.Status.COMPLETED)

    def test_the_tier_is_the_one_core_offered_not_the_one_the_report_names(self):
        attempt = self.attempt(tier=self.verified)
        self.report("checkout_completed", "evt_1", checkout_attempt_id=attempt.public_id,
                    tier_key="featured", period_end=T0 + 30 * DAY, payment_ref=REF)
        self.assertEqual(self.ent().tier, self.verified)

    def test_a_trial(self):
        attempt = self.attempt()
        self.report("checkout_completed", "evt_1", checkout_attempt_id=attempt.public_id,
                    trial_ends_at=T0 + 7 * DAY, payment_ref=REF)
        ent = self.ent()
        self.assertEqual((ent.status, ent.trial_ends_at), (S.TRIALING, T0 + 7 * DAY))

    def test_every_event_carries_the_adapter_as_origin_and_actor(self):
        self.subscribe()
        for event in self.events():
            if event.type.startswith(("entitlement.", "billing.", "listing.tier_changed")):
                self.assertEqual(event.origin, ADAPTER_ID, event.type)
                self.assertEqual(event.actor, {"type": "adapter", "id": ADAPTER_ID}, event.type)

    def test_a_checkout_core_never_started_is_ignored_and_changes_nothing(self):
        result = self.report("checkout_completed", "evt_1", checkout_attempt_id="chk_NOPE",
                             period_end=T0 + DAY, payment_ref=REF)
        self.assertEqual((result.outcome, result.reason), ("ignored", "unknown_checkout"))
        self.assertFalse(Entitlement.all_tenants.exists())
        self.assertEqual(PaymentReceipt.all_tenants.get().outcome, "ignored")

    def test_a_checkout_for_another_listing_is_refused(self):
        other = self.make_listing("other", "Other")
        attempt = self.attempt(listing=other)
        result = self.report("checkout_completed", "evt_1", checkout_attempt_id=attempt.public_id,
                             listing_id=self.listing.public_id, period_end=T0 + DAY, payment_ref=REF)
        self.assertEqual(result.reason, "checkout_mismatch")
        self.assertFalse(Entitlement.all_tenants.exists())

    def test_a_checkout_cannot_be_completed_twice(self):
        attempt = self.attempt()
        kw = dict(checkout_attempt_id=attempt.public_id, period_end=T0 + 30 * DAY, payment_ref=REF)
        self.report("checkout_completed", "evt_1", **kw)
        result = self.report("checkout_completed", "evt_2", **kw)
        self.assertEqual((result.outcome, result.reason), ("ignored", "checkout_already_completed"))
        self.assertEqual(self.events("entitlement.started").count(), 1)

    def test_a_report_missing_its_period_is_a_bad_report(self):
        attempt = self.attempt()
        result = self.report("checkout_completed", "evt_1", checkout_attempt_id=attempt.public_id)
        self.assertEqual(result.reason, "bad_report")
        self.assertFalse(Entitlement.all_tenants.exists())

    def test_a_checkout_for_a_listing_already_paying_is_ignored_but_finishes_the_attempt(self):
        self.subscribe("evt_a")
        second = self.attempt()
        result = self.report("checkout_completed", "evt_b", checkout_attempt_id=second.public_id,
                             period_end=T0 + 60 * DAY, payment_ref=REF)
        self.assertEqual((result.outcome, result.reason), ("ignored", "invalid_transition"))
        second.refresh_from_db()
        self.assertEqual(second.status, CheckoutAttempt.Status.COMPLETED)
        self.assertEqual(self.ent().current_period_end, T0 + 30 * DAY)


class PaymentOutcomeTests(_Base):
    def test_a_successful_invoice_renews_and_emits_a_billing_event(self):
        self.subscribe()
        self.report("payment_succeeded", "evt_2", listing_id=self.listing.public_id,
                    period_end=T0 + 60 * DAY, amount_minor=2900, currency="USD", invoice_ref="in_1")
        self.assertEqual(self.ent().current_period_end, T0 + 60 * DAY)
        data = self.events("billing.payment_succeeded").get().data
        self.assertEqual((data["amount"], data["currency"], data["invoice_ref"]), (2900, "USD", "in_1"))

    def test_the_first_invoice_can_arrive_before_the_checkout_completion(self):
        attempt = self.attempt()
        self.report("payment_succeeded", "evt_1", checkout_attempt_id=attempt.public_id,
                    period_end=T0 + 30 * DAY, payment_ref=REF)
        self.assertEqual(self.ent().status, S.ACTIVE)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, CheckoutAttempt.Status.COMPLETED)
        late = self.report("checkout_completed", "evt_2", checkout_attempt_id=attempt.public_id,
                           period_end=T0 + 30 * DAY, payment_ref=REF)
        self.assertEqual(late.outcome, "ignored")
        self.assertEqual(self.events("entitlement.started").count(), 1)

    def test_an_invoice_for_a_listing_with_no_entitlement_and_no_checkout_is_ignored(self):
        result = self.report("payment_succeeded", "evt_1", listing_id=self.listing.public_id,
                             period_end=T0 + DAY)
        self.assertEqual(result.reason, "no_entitlement")

    def test_a_failure_starts_dunning_and_a_later_success_recovers(self):
        self.subscribe()
        self.report("payment_failed", "evt_f", listing_id=self.listing.public_id,
                    failure_code="card_declined", attempt=1, now=T0 + 30 * DAY)
        ent = self.ent()
        self.assertEqual((ent.status, ent.last_failure_code), (S.PAST_DUE, "card_declined"))
        failed = self.events("billing.payment_failed").get().data
        self.assertEqual((failed["attempt"], failed["failure_code"]), (1, "card_declined"))
        self.report("payment_succeeded", "evt_ok", listing_id=self.listing.public_id,
                    period_end=T0 + 60 * DAY, now=T0 + 33 * DAY)
        self.assertEqual(self.ent().status, S.ACTIVE)
        self.assertTrue(self.events("entitlement.recovered").exists())

    def test_the_full_declined_card_path(self):
        self.subscribe()
        self.report("payment_failed", "evt_f", listing_id=self.listing.public_id, failure_code="x")
        self.assertEqual(self.fresh().current_tier, self.featured)  # past_due keeps perks
        from billing import jobs

        jobs.entitlement_transitions(now=T0 + DUNNING)
        self.assertEqual(self.ent().status, S.GRACE)
        self.assertEqual(self.fresh().current_tier, self.free)
        self.report("payment_succeeded", "evt_paid", listing_id=self.listing.public_id,
                    period_end=T0 + 90 * DAY, now=T0 + DUNNING + 2 * DAY)
        self.assertEqual(self.ent().status, S.ACTIVE)
        self.assertEqual(self.fresh().current_tier, self.featured)
        self.assertTrue(self.events("entitlement.restored").exists())

    def test_a_refund_expires_it_immediately(self):
        self.subscribe()
        self.report("refunded", "evt_r", listing_id=self.listing.public_id, amount_minor=2900,
                    currency="USD", reason="requested")
        self.assertEqual(self.ent().status, S.EXPIRED)
        data = self.events("billing.refund_issued").get().data
        self.assertEqual((data["amount"], data["reason"]), (2900, "requested"))
        self.assertEqual(self.fresh().visibility, "published")

    def test_a_subscription_change_moves_the_tier(self):
        self.subscribe()
        self.report("subscription_changed", "evt_c", listing_id=self.listing.public_id,
                    tier_key="verified", period_end=T0 + 30 * DAY)
        self.assertEqual((self.ent().tier, self.ent().status), (self.verified, S.ACTIVE))
        self.assertEqual(self.fresh().current_tier, self.verified)
        data = self.events("billing.subscription_changed").get().data
        self.assertEqual((data["from_plan"], data["to_plan"]), ("featured", "verified"))
        self.assertEqual(self.events("listing.tier_changed").order_by("id").last().data["cause"], "downgrade")

    def test_a_cancellation_at_period_end_and_an_outright_end(self):
        self.subscribe()
        self.report("subscription_canceled", "evt_c1", listing_id=self.listing.public_id,
                    at_period_end=True, canceled_by="owner")
        self.assertEqual(self.ent().status, S.CANCELED)
        self.assertEqual(self.fresh().current_tier, self.featured)
        other = self.make_listing("o2", "O2")
        attempt = self.attempt(listing=other)
        self.report("checkout_completed", "evt_s2", checkout_attempt_id=attempt.public_id,
                    period_end=T0 + 30 * DAY, payment_ref=REF)
        self.report("subscription_canceled", "evt_c2", listing_id=other.public_id, at_period_end=False)
        self.assertEqual(self.ent(other).status, S.EXPIRED)
        self.assertEqual(self.events("billing.subscription_canceled").count(), 2)

    def test_the_providers_echo_of_a_cancel_core_already_made_is_a_quiet_no_op(self):
        self.subscribe()
        self.report("subscription_canceled", "evt_1", listing_id=self.listing.public_id)
        result = self.report("subscription_canceled", "evt_2", listing_id=self.listing.public_id)
        self.assertEqual((result.outcome, result.reason), ("ignored", "invalid_transition"))

    def test_unknown_things_are_ignored_not_errors(self):
        cases = [
            (dict(kind="payment_failed", listing_id="listing_NOPE"), "unknown_listing"),
            (dict(kind="payment_failed"), "unknown_listing"),
            (dict(kind="bogus", listing_id="x"), "unknown_kind"),
            (dict(kind="payment_failed", listing_id="x", event_id=""), "bad_report"),
        ]
        for i, (kind_kw, reason) in enumerate(cases):
            with self.subTest(reason=reason, i=i):
                kw = dict(kind_kw)
                kind = kw.pop("kind")
                eid = kw.pop("event_id", f"evt-{i}-{reason}")
                result = self.report(kind, eid, **kw)
                self.assertEqual((result.outcome, result.reason), ("ignored", reason))
        result = self.report("subscription_changed", "evt_x", listing_id=self.listing.public_id,
                             tier_key="nope", period_end=T0 + DAY)
        self.assertEqual(result.outcome, "ignored")

    def test_a_report_for_a_comp_is_refused_by_the_state_machine(self):
        self.apply(machine.Trigger.GRANT, tier=self.featured, operator=self.operator(3), reason="r")
        result = self.report("refunded", "evt_1", listing_id=self.listing.public_id)
        self.assertEqual(result.reason, "invalid_transition")
        self.assertEqual(self.ent().status, S.COMPED)


class IdempotencyTests(_Base):
    def test_a_redelivery_applies_once(self):
        attempt = self.attempt()
        kw = dict(checkout_attempt_id=attempt.public_id, period_end=T0 + 30 * DAY, payment_ref=REF)
        first = self.report("checkout_completed", "evt_1", **kw)
        since = self.last_event_id()
        again = self.report("checkout_completed", "evt_1", **kw)
        self.assertEqual((first.outcome, again.outcome), ("applied", "replayed"))
        self.assertEqual(self.events(since=since).count(), 0)
        self.assertEqual(PaymentReceipt.all_tenants.count(), 1)
        log = CommandLog.objects.filter(command="entitlement.reportPayment").order_by("id")
        self.assertEqual([r.outcome for r in log], ["applied", "applied"])
        self.assertEqual(log.last().problem, {"idempotent_replay": True})
        self.assertEqual(log.first().idempotency_key, f"{ADAPTER_ID}:evt_1")

    def test_an_ignored_event_redelivered_is_a_replay_not_a_second_log_row(self):
        self.report("payment_failed", "evt_1", listing_id="listing_NOPE")
        rows = CommandLog.objects.filter(command="entitlement.reportPayment").count()
        again = self.report("payment_failed", "evt_1", listing_id="listing_NOPE")
        self.assertEqual(again.outcome, "replayed")
        self.assertEqual(CommandLog.objects.filter(command="entitlement.reportPayment").count(), rows)

    def test_the_receipt_is_unique_per_tenant_adapter_and_event(self):
        PaymentReceipt.all_tenants.create(tenant=self.tenant, adapter_id="a", external_event_id="e",
                                          kind="k", outcome="applied")
        with self.assertRaises(IntegrityError):
            PaymentReceipt.all_tenants.create(tenant=self.tenant, adapter_id="a", external_event_id="e",
                                              kind="k", outcome="applied")
        PaymentReceipt.all_tenants.create(tenant=self.tenant, adapter_id="b", external_event_id="e",
                                          kind="k", outcome="applied")

    def test_a_concurrent_delivery_that_loses_the_receipt_race_is_a_replay(self):
        # The receipt appears between the early lookup and the insert.
        attempt = self.attempt()
        original = reports._apply

        def racing(tenant, **kw):
            PaymentReceipt.all_tenants.create(
                tenant=tenant, adapter_id=kw["adapter_id"],
                external_event_id=kw["report"].external_event_id, kind="x", outcome="applied",
            )
            return original(tenant, **kw)

        reports._apply = racing
        try:
            result = self.report("checkout_completed", "evt_1", checkout_attempt_id=attempt.public_id,
                                 period_end=T0 + DAY, payment_ref=REF)
        finally:
            reports._apply = original
        self.assertEqual(result.outcome, "replayed")
        self.assertFalse(Entitlement.all_tenants.exists())

    def test_the_command_log_keeps_ids_and_the_kind_only(self):
        self.subscribe()
        row = CommandLog.objects.filter(command="entitlement.reportPayment").first()
        self.assertEqual(set(row.payload), {"kind", "listing_id", "checkout_attempt_id", "tier"})
        self.assertEqual(row.actor, {"type": "adapter", "id": ADAPTER_ID})
        self.assertEqual(row.adapter_id, ADAPTER_ID)

    def test_billing_events_are_subject_to_the_listing(self):
        self.subscribe()
        for e in OutboxEvent.all_tenants.filter(type__startswith="billing."):
            self.assertEqual(e.subject, self.listing.public_id)
