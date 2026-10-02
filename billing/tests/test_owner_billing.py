"""The owner's billing pages and the §6.5 banners (decisions.md §4.11)."""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, override_settings
from django.utils import timezone

from billing import machine
from billing.models import CheckoutAttempt, Entitlement
from billing.tests.base import DAY, T0, Role
from billing.tests.payments_base import ADAPTER_ID, PaymentTestCase
from directory.models import DirectoryUser, Listing
from osds.adapters import override_capability
from tenants.models import InstallSetup

S = Entitlement.Status
REF = {"adapter": ADAPTER_ID, "external_id": "sub_1"}


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class OwnerBillingTests(PaymentTestCase):
    def setUp(self):
        super().setUp()
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.client = self.owner_client()
        self.page_url = f"/owner/listings/{self.listing.public_id}/"

    def page(self, client=None):
        return (client or self.client).get(self.page_url, HTTP_HOST="acme.test")

    def post(self, suffix, data=None, client=None):
        return (client or self.client).post(
            self.page_url + "billing/" + suffix, data or {}, HTTP_HOST="acme.test"
        )

    def now_apply(self, trigger, **kw):
        kw.setdefault("now", timezone.now())
        return self.apply(trigger, **kw)

    def subscribe(self, **kw):
        self.now_apply(machine.Trigger.START, tier=self.featured,
                       period_end=timezone.now() + 30 * DAY, payment_ref=REF, **kw)

    # -- the §6.5 table --------------------------------------------------------
    def test_no_plan_offers_the_upgrade(self):
        page = self.page()
        self.assertContains(page, "Upgrade your listing.")
        self.assertContains(page, "Upgrade to Featured")
        self.assertContains(page, "2900 USD / month")
        self.assertContains(page, "7-day trial")
        self.assertContains(page, "Upgrade to Verified")
        self.assertNotContains(page, "Cancel plan")

    def test_no_offer_when_payments_are_unavailable(self):
        self.provider._available = False
        page = self.page()
        self.assertNotContains(page, "Upgrade")
        self.assertNotContains(page, "Your plan")

    def test_trialing_says_how_long_is_left(self):
        self.now_apply(machine.Trigger.START_TRIAL, tier=self.featured,
                       trial_ends_at=timezone.now() + 7 * DAY + timedelta(hours=1), payment_ref=REF)
        page = self.page()
        self.assertContains(page, "Trial ends in 7 days.")
        self.assertContains(page, "Cancel plan")

    def test_active_is_normal(self):
        self.subscribe()
        page = self.page()
        self.assertContains(page, "Featured, renews")
        self.assertContains(page, "Cancel plan")
        self.assertNotContains(page, "Payment failed")
        self.assertNotContains(page, "Upgrade to")

    def test_past_due_is_a_persistent_banner_with_a_card_link(self):
        self.subscribe()
        self.now_apply(machine.Trigger.PAYMENT_FAILED, failure_code="x")
        for url in (self.page_url, "/owner/"):
            self.assertContains(self.client.get(url, HTTP_HOST="acme.test"), "Payment failed, update card.")
        self.assertContains(self.page(), "Update card")
        self.assertContains(self.page(), f"{self.page_url}billing/portal/")

    def test_grace_says_it_was_downgraded_and_offers_restore(self):
        self.subscribe()
        self.now_apply(machine.Trigger.PAYMENT_FAILED, failure_code="x")
        Entitlement.all_tenants.update(dunning_ends_at=timezone.now())
        self.now_apply(machine.Trigger.DUNNING_ELAPSED)
        page = self.page()
        self.assertContains(page, "Your listing has been downgraded. Restore it.")
        self.assertContains(page, "Restore")
        self.assertContains(self.client.get("/owner/", HTTP_HOST="acme.test"), "has been downgraded")

    def test_expired_is_an_upgrade_prompt(self):
        self.subscribe()
        self.now_apply(machine.Trigger.REFUND)
        self.assertContains(self.page(), "Your upgraded placement has ended. Upgrade to get it back.")

    def test_canceled_says_until_when_and_may_resubscribe(self):
        self.subscribe()
        self.now_apply(machine.Trigger.CANCEL, canceled_by="owner")
        page = self.page()
        self.assertContains(page, "Cancelled, active until")
        self.assertContains(page, "Upgrade to Featured")
        self.assertNotContains(page, "Cancel plan")

    def test_a_comp_shows_nothing_about_billing_at_all(self):
        self.now_apply(machine.Trigger.GRANT, tier=self.featured, operator=self.operator(Role.MANAGER),
                       reason="secret partner deal")
        page = self.page()
        for text in ("Your plan", "Upgrade", "Cancel plan", "comp", "secret partner deal", "Featured, renews"):
            self.assertNotContains(page, text)
        self.assertNotContains(self.client.get("/owner/", HTTP_HOST="acme.test"), "Payment failed")

    # -- the actions -----------------------------------------------------------
    def test_upgrade_redirects_to_the_providers_checkout(self):
        r = self.post("checkout/", {"tier": "featured"})
        attempt = CheckoutAttempt.all_tenants.get()
        self.assertEqual((r.status_code, r["Location"]), (302, f"https://stubpay.test/pay/{attempt.public_id}"))

    def test_a_refused_upgrade_comes_back_with_the_reason(self):
        r = self.client.post(self.page_url + "billing/checkout/", {"tier": "free"}, HTTP_HOST="acme.test", follow=True)
        self.assertContains(r, "That tier is not for sale.")
        self.provider.fail_checkout = True
        r = self.client.post(self.page_url + "billing/checkout/", {"tier": "featured"}, HTTP_HOST="acme.test", follow=True)
        self.assertContains(r, "could not complete that")

    def test_cancel_goes_through_the_command(self):
        self.subscribe()
        r = self.client.post(self.page_url + "billing/cancel/", {}, HTTP_HOST="acme.test", follow=True)
        self.assertContains(r, "Your plan is cancelled.")
        self.assertEqual(Entitlement.all_tenants.get().status, S.CANCELED)
        self.assertEqual(self.provider.cancels, [(REF, True)])

    def test_a_failed_cancel_says_so_and_changes_nothing(self):
        self.subscribe()
        self.provider.fail_cancel = True
        r = self.client.post(self.page_url + "billing/cancel/", {}, HTTP_HOST="acme.test", follow=True)
        self.assertContains(r, "could not complete that")
        self.assertEqual(Entitlement.all_tenants.get().status, S.ACTIVE)

    def test_the_portal_redirects_to_the_providers_page(self):
        self.subscribe()
        r = self.client.get(self.page_url + "billing/portal/", HTTP_HOST="acme.test")
        self.assertEqual((r.status_code, r["Location"]), (302, self.provider.portal))
        self.assertEqual(self.provider.portals[0][1], f"https://acme.test{self.page_url}")

    def test_the_portal_with_nothing_to_open_goes_back(self):
        r = self.client.get(self.page_url + "billing/portal/", HTTP_HOST="acme.test", follow=True)
        self.assertContains(r, "no payment page to open")

    def test_the_return_page_promises_nothing_it_cannot_know(self):
        ok = self.client.get(self.page_url + "billing/return/?checkout=success", HTTP_HOST="acme.test")
        self.assertContains(ok, "being confirmed")
        self.assertEqual(Entitlement.all_tenants.count(), 0)  # visiting changes no state
        self.assertContains(self.client.get(self.page_url + "billing/return/?checkout=cancel", HTTP_HOST="acme.test"),
                            "Nothing was charged")

    # -- access ----------------------------------------------------------------
    def test_routes_need_an_owner_session_and_are_post_only_where_they_act(self):
        anon = Client()
        for suffix in ("checkout/", "cancel/"):
            r = anon.post(self.page_url + "billing/" + suffix, {}, HTTP_HOST="acme.test")
            self.assertEqual((r.status_code, r["Location"]), (302, "/owner/signin/"))
        self.assertEqual(self.client.get(self.page_url + "billing/checkout/", HTTP_HOST="acme.test").status_code, 405)
        self.assertEqual(self.client.get(self.page_url + "billing/cancel/", HTTP_HOST="acme.test").status_code, 405)
        self.assertEqual(self.client.post(self.page_url + "billing/portal/", {}, HTTP_HOST="acme.test").status_code, 405)

    def test_someone_elses_listing_is_404_on_every_route(self):
        other = self.make_listing("other", "Other")
        for suffix, method in (("checkout/", "post"), ("cancel/", "post"), ("portal/", "get"), ("return/", "get")):
            with self.subTest(suffix=suffix):
                r = getattr(self.client, method)(f"/owner/listings/{other.public_id}/billing/{suffix}",
                                                 HTTP_HOST="acme.test")
                self.assertEqual(r.status_code, 404)
        self.assertEqual(CheckoutAttempt.all_tenants.count(), 0)

    def test_the_actions_need_csrf(self):
        strict = Client(enforce_csrf_checks=True)
        strict.cookies = self.client.cookies
        self.assertEqual(self.post("checkout/", {"tier": "featured"}, client=strict).status_code, 403)
        self.assertEqual(self.post("cancel/", client=strict).status_code, 403)

    def test_an_operator_session_is_not_an_owner_session(self):
        operator = Client()
        operator.force_login(self.operator(Role.ADMIN))
        r = self.post("checkout/", {"tier": "featured"}, client=operator)
        self.assertEqual((r.status_code, r["Location"]), (302, "/owner/signin/"))

    def test_the_billing_routes_exist_for_no_provider_too(self):
        with override_capability({}):
            self.assertEqual(self.page().status_code, 200)
            r = self.client.post(self.page_url + "billing/checkout/", {"tier": "featured"},
                                 HTTP_HOST="acme.test", follow=True)
            self.assertContains(r, "Payments are not available")
