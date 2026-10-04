"""``billing.start_checkout`` and ``entitlement.cancel`` against the stub
provider (decisions.md §4.11)."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from audit.models import CommandLog
from audit.ratelimit import RateLimited
from audit.tests.window_clock import pinned_windows
from billing import machine, payments
from billing.models import CheckoutAttempt, Entitlement, Tier
from billing.tests.base import DAY, T0, Role
from billing.tests.payments_base import ADAPTER_ID, PaymentTestCase
from directory.models import DirectoryUser, Listing
from osds.adapters import override_capability
from osds.tenancy import tenant_context
from tenants.models import Tenant

S = Entitlement.Status
REF = {"adapter": ADAPTER_ID, "external_id": "sub_1"}


class _Base(PaymentTestCase):
    def setUp(self):
        super().setUp()
        # The eleven attempts must land in one 1-hour window (#245).
        self.enterContext(pinned_windows())

    def checkout(self, tier_key="featured", user=None, listing=None):
        with tenant_context(self.tenant):
            return payments.start_checkout(
                self.tenant, listing=listing or self.listing, user=user or self.owner, tier_key=tier_key
            )

    def refused(self, **kw):
        with self.assertRaises(payments.PaymentRefused) as cm:
            self.checkout(**kw)
        return cm.exception.reason

    def last_log(self, command):
        return CommandLog.objects.filter(command=command).order_by("id").last()

    def subscribe(self, listing=None):
        self.apply(machine.Trigger.START, listing=listing, tier=self.featured,
                   period_end=T0 + 30 * DAY, payment_ref=REF)


class StartCheckoutTests(_Base):
    def test_an_owner_gets_the_providers_url_and_core_decides_the_price(self):
        url = self.checkout()
        attempt = CheckoutAttempt.all_tenants.get()
        self.assertEqual(url, f"https://stubpay.test/pay/{attempt.public_id}")
        self.assertEqual((attempt.status, attempt.tier, attempt.listing, attempt.started_by),
                         ("started", self.featured, self.listing, self.owner))
        self.assertEqual(attempt.external_ref, f"cs_{attempt.public_id}")
        req = self.provider.checkouts[0]
        self.assertEqual((req.attempt_id, req.listing_id, req.tier_key, req.price_minor, req.currency,
                          req.interval, req.trial_days, req.customer_email),
                         (attempt.public_id, self.listing.public_id, "featured", 2900, "USD",
                          "month", 7, "owner@acme-co.example"))
        base = f"https://acme.test/owner/listings/{self.listing.public_id}/billing/return/"
        self.assertEqual(req.success_url, base + "?checkout=success")
        self.assertEqual(req.cancel_url, base + "?checkout=cancel")

    def test_it_emits_billing_checkout_started_with_the_owner_as_actor(self):
        self.checkout()
        event = self.events("billing.checkout_started").get()
        self.assertEqual(event.data, {"plan_id": "featured", "listing_id": self.listing.public_id,
                                      "amount": 2900, "currency": "USD"})
        self.assertEqual(event.actor, {"type": "owner", "id": self.owner.public_id})
        self.assertEqual(event.subject, self.listing.public_id)
        log = self.last_log("billing.start_checkout")
        self.assertEqual((log.outcome, log.result_event_id), ("applied", event.event_id))
        self.assertNotIn("owner@acme-co.example", str(log.payload))

    def test_the_guards(self):
        stranger = DirectoryUser.all_tenants.create(tenant=self.tenant, email="x@x.example")
        cases = [
            (dict(user=stranger), "not_owner"),
            (dict(tier_key="nope"), "tier_unknown"),
            (dict(tier_key=""), "tier_unknown"),
            (dict(tier_key="free"), "tier_not_for_sale"),
        ]
        for kw, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.refused(**kw), reason)
        self.assertFalse(CheckoutAttempt.all_tenants.exists())
        self.assertEqual(self.provider.checkouts, [])

    def test_a_tier_not_for_sale_or_slot_backed_is_refused(self):
        Tier.all_tenants.filter(pk=self.verified.pk).update(purchasable=False, price_minor=None,
                                                            currency="", interval="")
        self.assertEqual(self.refused(tier_key="verified"), "tier_not_for_sale")
        Tier.all_tenants.filter(pk=self.featured.pk).update(uses_slot=True)
        self.assertEqual(self.refused(tier_key="featured"), "tier_not_for_sale")

    def test_a_suspended_listing_cannot_buy(self):
        Listing.all_tenants.filter(pk=self.listing.pk).update(status=Listing.Status.SUSPENDED)
        self.assertEqual(self.refused(), "listing_suspended")

    def test_a_former_owner_cannot_buy(self):
        Listing.all_tenants.filter(pk=self.listing.pk).update(owner=None, status="unclaimed")
        self.assertEqual(self.refused(), "not_owner")

    def test_payments_must_be_available(self):
        with override_capability({}):
            self.assertEqual(self.refused(), "unavailable")
        self.provider._available = False
        self.assertEqual(self.refused(), "unavailable")
        self.provider._available = True
        Tenant.objects.filter(pk=self.tenant.pk).update(domain_verified_at=None)
        self.tenant.refresh_from_db()
        self.assertEqual(self.refused(), "unavailable")

    def test_a_paid_listing_does_not_buy_again_and_a_lapsing_one_is_sent_to_the_portal(self):
        self.subscribe()
        self.assertEqual(self.refused(), "already_subscribed")
        self.apply(machine.Trigger.PAYMENT_FAILED, failure_code="x", now=T0 + 30 * DAY)
        self.assertEqual(self.refused(), "use_portal")
        self.apply(machine.Trigger.DUNNING_ELAPSED, now=T0 + 44 * DAY)
        self.assertEqual(self.refused(), "use_portal")

    def test_an_expired_canceled_or_comped_listing_may_buy(self):
        for i, setup in enumerate(("expire", "cancel", "comp")):
            with self.subTest(setup=setup):
                listing = self.make_listing(f"l{i}", f"L{i}")
                Listing.all_tenants.filter(pk=listing.pk).update(owner=self.owner, status="claimed")
                if setup == "comp":
                    self.apply(machine.Trigger.GRANT, listing=listing, tier=self.verified,
                               operator=self.operator(Role.MANAGER), reason="r")
                else:
                    self.subscribe(listing)
                    trigger = machine.Trigger.REFUND if setup == "expire" else machine.Trigger.CANCEL
                    self.apply(trigger, listing=listing, canceled_by="owner")
                self.assertTrue(self.checkout(listing=listing).startswith("https://stubpay.test/pay/"))

    def test_a_provider_failure_marks_the_attempt_failed_and_emits_nothing(self):
        self.provider.fail_checkout = True
        self.assertEqual(self.refused(), "provider_error")
        self.assertEqual(CheckoutAttempt.all_tenants.get().status, "failed")
        self.assertFalse(self.events("billing.checkout_started").exists())
        self.assertEqual(self.last_log("billing.start_checkout").outcome, "rejected")

    def test_at_most_ten_attempts_an_hour_per_owner(self):
        for _ in range(10):
            self.checkout()
        with self.assertRaises(RateLimited):
            self.checkout()
        blocked = CommandLog.objects.filter(command="billing.start_checkout", outcome="blocked")
        self.assertEqual(blocked.count(), 1)
        self.assertEqual(len(self.provider.checkouts), 10)

    def test_an_attempt_lives_a_day(self):
        before = timezone.now()
        self.checkout()
        attempt = CheckoutAttempt.all_tenants.get()
        self.assertAlmostEqual((attempt.expires_at - before).total_seconds(), 86400, delta=30)

    def test_the_command_refuses_to_run_inside_a_transaction(self):
        from django.db import transaction

        from audit.command_log import MustNotBeInTransaction

        with transaction.atomic(), tenant_context(self.tenant), self.assertRaises(MustNotBeInTransaction):
            payments.start_checkout(self.tenant, listing=self.listing, user=self.owner, tier_key="featured")


class CancelTests(_Base):
    def cancel(self, **kw):
        kw.setdefault("by", "owner")
        if kw["by"] == "owner":
            kw.setdefault("user", self.owner)
        with tenant_context(self.tenant):
            return payments.cancel_entitlement(self.tenant, listing=self.listing, **kw)

    def refused_cancel(self, **kw):
        with self.assertRaises(payments.PaymentRefused) as cm:
            self.cancel(**kw)
        return cm.exception.reason

    def test_the_provider_is_asked_first_then_the_entitlement_is_canceled(self):
        self.subscribe()
        self.cancel(reason="closing")
        self.assertEqual(self.provider.cancels, [(REF, True)])
        ent = self.ent()
        self.assertEqual((ent.status, ent.canceled_by, ent.cancel_at_period_end), (S.CANCELED, "owner", True))
        data = self.events("billing.subscription_canceled").get().data
        self.assertEqual((data["at_period_end"], data["canceled_by"], data["reason"]), (True, "owner", "closing"))
        self.assertEqual(self.last_log("entitlement.cancel").outcome, "applied")
        self.assertEqual(self.fresh().current_tier, self.featured)  # until the period ends

    def test_a_provider_failure_changes_nothing(self):
        self.subscribe()
        self.provider.fail_cancel = True
        self.assertEqual(self.refused_cancel(), "provider_error")
        self.assertEqual(self.ent().status, S.ACTIVE)
        self.assertFalse(self.events("billing.subscription_canceled").exists())

    def test_a_paid_listing_cannot_be_canceled_with_no_provider_to_stop_the_billing(self):
        self.subscribe()
        with override_capability({}):
            self.assertEqual(self.refused_cancel(), "unavailable")
        self.assertEqual(self.ent().status, S.ACTIVE)

    def test_from_grace_it_ends_at_once_and_the_provider_is_told_not_to_wait(self):
        self.subscribe()
        self.apply(machine.Trigger.PAYMENT_FAILED, failure_code="x", now=T0 + 30 * DAY)
        self.apply(machine.Trigger.DUNNING_ELAPSED, now=T0 + 44 * DAY)
        self.cancel()
        self.assertEqual(self.provider.cancels[-1][1], False)
        self.assertEqual(self.ent().status, S.EXPIRED)

    def test_from_past_due_it_skips_grace(self):
        self.subscribe()
        self.apply(machine.Trigger.PAYMENT_FAILED, failure_code="x", now=T0 + 30 * DAY)
        self.cancel()
        self.assertEqual(self.ent().status, S.CANCELED)

    def test_an_entitlement_without_a_provider_reference_is_canceled_without_a_call(self):
        self.apply(machine.Trigger.START, tier=self.featured, period_end=T0 + 30 * DAY)
        self.cancel()
        self.assertEqual(self.provider.cancels, [])
        self.assertEqual(self.ent().status, S.CANCELED)

    def test_what_cannot_be_canceled(self):
        self.assertEqual(self.refused_cancel(), "not_cancelable")  # no entitlement
        self.apply(machine.Trigger.GRANT, tier=self.featured, operator=self.operator(Role.MANAGER), reason="r")
        self.assertEqual(self.refused_cancel(), "not_cancelable")  # a comp is revoked, not cancelled

    def test_only_the_live_owner_may_cancel_as_the_owner(self):
        self.subscribe()
        stranger = DirectoryUser.all_tenants.create(tenant=self.tenant, email="x@x.example")
        self.assertEqual(self.refused_cancel(user=stranger), "not_owner")
        self.assertEqual(self.last_log("entitlement.cancel").outcome, "blocked")
        Listing.all_tenants.filter(pk=self.listing.pk).update(owner=None)
        self.assertEqual(self.refused_cancel(), "not_owner")
        self.assertEqual(self.ent().status, S.ACTIVE)

    def test_a_manager_may_cancel_for_the_owner_and_below_that_is_blocked(self):
        self.subscribe()
        self.assertEqual(
            self.refused_cancel(by="operator", operator=self.operator(Role.EDITOR)), "forbidden"
        )
        self.assertEqual(self.last_log("entitlement.cancel").outcome, "blocked")
        self.cancel(by="operator", operator=self.operator(Role.MANAGER), reason="owner asked by phone")
        self.assertEqual(self.ent().canceled_by, "operator")
        self.assertEqual(self.last_log("entitlement.cancel").actor["type"], "staff")

    def test_the_portal_link(self):
        self.subscribe()
        with tenant_context(self.tenant):
            url = payments.portal_url(self.tenant, listing=self.listing, user=self.owner,
                                      return_url="https://acme.test/owner/")
        self.assertEqual(url, self.provider.portal)
        self.assertEqual(self.provider.portals[0], (REF, "https://acme.test/owner/"))
        other = self.make_listing("no-sub", "No sub")
        Listing.all_tenants.filter(pk=other.pk).update(owner=self.owner)
        with tenant_context(self.tenant):
            self.assertIsNone(payments.portal_url(self.tenant, listing=other, user=self.owner, return_url="x"))
        stranger = DirectoryUser.all_tenants.create(tenant=self.tenant, email="y@y.example")
        with tenant_context(self.tenant), self.assertRaises(payments.PaymentRefused):
            payments.portal_url(self.tenant, listing=self.listing, user=stranger, return_url="x")
