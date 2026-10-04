"""The Stripe provider against a scripted ``ctx.http`` (no network, no core)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

from django.test import SimpleTestCase

from adapters.stripe.provider import StripeProvider
from adapters.tests.stripe_fakes import (
    NOW, SECRET_KEY, WEBHOOK_SECRET, FakeHttp, make_ctx, sign,
)
from osds.adapter_api import CheckoutRequest, InboundRequest, PaymentError

META = {"attempt_id": "chk_1", "listing_id": "listing_1", "tier": "featured"}
PERIOD_END = 1_800_000_000
DT = datetime.fromtimestamp(PERIOD_END, tz=timezone.utc)


def checkout_request(**over):
    base = dict(
        attempt_id="chk_1", listing_id="listing_1", listing_name="Acme", tier_key="featured",
        tier_name="Featured", price_minor=4900, currency="USD", interval="month",
        trial_days=None, customer_email="owner@example.com",
        success_url="https://acme.test/ok", cancel_url="https://acme.test/no",
    )
    base.update(over)
    return CheckoutRequest(**base)


def price(**over):
    base = {"id": "price_FEAT", "active": True, "unit_amount": 4900, "currency": "usd",
            "recurring": {"interval": "month", "interval_count": 1}}
    base.update(over)
    return base


def event(type_, obj, id_="evt_1", **extra):
    return {"id": id_, "type": type_, "data": {"object": obj, **extra}}


def webhook(provider, ctx, payload, *, header=None, method="POST"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {"stripe-signature": header if header is not None else sign(body)}
    return provider.inbound(ctx, InboundRequest(method, headers, body))


class SettingsTests(SimpleTestCase):
    def test_available_needs_both_secrets(self):
        p = StripeProvider()
        self.assertTrue(p.available(make_ctx()))
        self.assertFalse(p.available(make_ctx(secrets={"secret_key": SECRET_KEY})))
        self.assertFalse(p.available(make_ctx(secrets={"webhook_secret": WEBHOOK_SECRET})))
        self.assertFalse(p.available(make_ctx(secrets={})))

    def test_fields(self):
        fields = {f.key: f for f in StripeProvider().settings_fields()}
        self.assertEqual(set(fields), {"secret_key", "webhook_secret", "price_ids"})
        self.assertTrue(fields["secret_key"].secret and fields["webhook_secret"].secret)
        self.assertFalse(fields["price_ids"].secret)

    def test_patterns(self):
        import re

        fields = {f.key: f.pattern for f in StripeProvider().settings_fields()}
        ok = lambda key, v: bool(re.fullmatch(fields[key], v))
        self.assertTrue(ok("secret_key", "sk_test_abc123"))
        self.assertTrue(ok("secret_key", "rk_live_abc123"))
        self.assertFalse(ok("secret_key", "pk_test_abc123"))
        self.assertFalse(ok("secret_key", "sk_test_"))
        self.assertTrue(ok("webhook_secret", "whsec_abc123"))
        self.assertFalse(ok("webhook_secret", "sk_test_abc"))
        self.assertTrue(ok("price_ids", "featured=price_1ABC"))
        self.assertTrue(ok("price_ids", "featured=price_1ABC, verified=price_1DEF"))
        self.assertFalse(ok("price_ids", "featured=prod_1ABC"))
        self.assertFalse(ok("price_ids", "featured price_1ABC"))
        self.assertFalse(ok("price_ids", "featured=price_1ABC,"))

    def test_egress_is_stripe_only(self):
        self.assertEqual(tuple(StripeProvider.egress_allowlist), ("api.stripe.com",))


class CheckoutTests(SimpleTestCase):
    def setUp(self):
        self.http = FakeHttp()
        self.ctx = make_ctx(self.http)
        self.p = StripeProvider()
        self.http.add("GET", "prices/price_FEAT", price())
        self.http.add("POST", "checkout/sessions", {"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1"})

    def form(self):
        call = [c for c in self.http.calls if c["method"] == "POST"][-1]
        return {k: v[0] for k, v in parse_qs(call["body"].decode()).items()}, call

    def test_creates_a_subscription_session(self):
        result = self.p.create_checkout(self.ctx, checkout_request())
        self.assertEqual((result.url, result.external_ref), ("https://checkout.stripe.com/c/cs_1", "cs_1"))
        form, call = self.form()
        self.assertEqual(form["mode"], "subscription")
        self.assertEqual(form["line_items[0][price]"], "price_FEAT")
        self.assertEqual(form["line_items[0][quantity]"], "1")
        self.assertEqual(form["client_reference_id"], "chk_1")
        self.assertEqual(form["customer_email"], "owner@example.com")
        self.assertEqual(form["payment_method_collection"], "always")
        self.assertEqual(form["metadata[attempt_id]"], "chk_1")
        self.assertEqual(form["subscription_data[metadata][listing_id]"], "listing_1")
        self.assertEqual(form["subscription_data[metadata][tier]"], "featured")
        self.assertNotIn("subscription_data[trial_period_days]", form)
        self.assertEqual(call["headers"]["Idempotency-Key"], "chk_1")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {SECRET_KEY}")
        self.assertTrue(call["url"].startswith("https://api.stripe.com/v1/"))

    def test_a_trial_is_passed_through(self):
        self.p.create_checkout(self.ctx, checkout_request(trial_days=14))
        self.assertEqual(self.form()[0]["subscription_data[trial_period_days]"], "14")

    def test_a_tier_without_a_price_id_is_refused_before_any_call(self):
        with self.assertRaises(PaymentError):
            self.p.create_checkout(self.ctx, checkout_request(tier_key="gold"))
        self.assertEqual(self.http.calls, [])

    def test_no_price_ids_at_all(self):
        with self.assertRaises(PaymentError):
            self.p.create_checkout(make_ctx(self.http, config={}), checkout_request())

    def test_a_price_that_does_not_match_the_tier_is_refused(self):
        cases = {
            "amount": price(unit_amount=5900),
            "currency": price(currency="eur"),
            "interval": price(recurring={"interval": "year", "interval_count": 1}),
            "interval_count": price(recurring={"interval": "month", "interval_count": 3}),
            "inactive": price(active=False),
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.http.calls.clear()
                self.http.add("GET", "prices/price_FEAT", body)
                with self.assertRaises(PaymentError):
                    self.p.create_checkout(self.ctx, checkout_request())
                self.assertFalse([c for c in self.http.calls if c["method"] == "POST"])

    def test_a_stripe_error_carries_type_and_code_never_the_message_or_key(self):
        self.http.add("POST", "checkout/sessions", {"error": {
            "type": "invalid_request_error", "code": "parameter_invalid",
            "message": f"bad owner@example.com {SECRET_KEY}"}}, status=400)
        with self.assertRaises(PaymentError) as cm:
            self.p.create_checkout(self.ctx, checkout_request())
        text = str(cm.exception)
        self.assertIn("400", text)
        self.assertIn("parameter_invalid", text)
        self.assertNotIn("owner@example.com", text)
        self.assertNotIn(SECRET_KEY, text)

    def test_a_response_without_a_url_is_an_error(self):
        self.http.add("POST", "checkout/sessions", {"id": "cs_1"})
        with self.assertRaises(PaymentError):
            self.p.create_checkout(self.ctx, checkout_request())

    def test_the_price_id_is_validated_as_a_path_segment(self):
        ctx = make_ctx(self.http, config={"price_ids": "featured=price_../../x"})
        with self.assertRaises(PaymentError):
            self.p.create_checkout(ctx, checkout_request())
        self.assertEqual(self.http.calls, [])


class SubscriptionTests(SimpleTestCase):
    def setUp(self):
        self.http = FakeHttp()
        self.ctx = make_ctx(self.http)
        self.p = StripeProvider()
        self.ref = {"adapter": "stripe", "external_id": "sub_1", "customer": "cus_1"}

    def test_cancel_at_period_end(self):
        self.http.add("POST", "subscriptions/sub_1", {"id": "sub_1"})
        self.p.cancel_subscription(self.ctx, self.ref, at_period_end=True)
        call = self.http.calls[-1]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(parse_qs(call["body"].decode()), {"cancel_at_period_end": ["true"]})

    def test_cancel_immediately(self):
        self.http.add("DELETE", "subscriptions/sub_1", {"id": "sub_1"})
        self.p.cancel_subscription(self.ctx, self.ref, at_period_end=False)
        self.assertEqual(self.http.calls[-1]["method"], "DELETE")

    def test_an_already_gone_subscription_is_success(self):
        self.p.cancel_subscription(self.ctx, self.ref, at_period_end=True)  # 404 resource_missing
        self.p.cancel_subscription(self.ctx, self.ref, at_period_end=False)

    def test_other_errors_propagate(self):
        self.http.add("POST", "subscriptions/sub_1", {"error": {"type": "api_error"}}, status=500)
        with self.assertRaises(PaymentError):
            self.p.cancel_subscription(self.ctx, self.ref, at_period_end=True)

    def test_a_stored_reference_cannot_redirect_the_call(self):
        for bad in ("sub_1/../customers", "cus_1", "", "sub_1?x=y"):
            with self.subTest(bad=bad):
                with self.assertRaises(PaymentError):
                    self.p.cancel_subscription(
                        self.ctx, {"external_id": bad}, at_period_end=True)
        self.assertEqual(self.http.calls, [])

    def test_portal_url(self):
        self.http.add("POST", "billing_portal/sessions", {"url": "https://billing.stripe.com/p/x"})
        url = self.p.portal_url(self.ctx, self.ref, "https://acme.test/billing")
        self.assertEqual(url, "https://billing.stripe.com/p/x")
        form = parse_qs(self.http.calls[-1]["body"].decode())
        self.assertEqual(form["customer"], ["cus_1"])
        self.assertEqual(form["return_url"], ["https://acme.test/billing"])

    def test_no_customer_no_portal(self):
        self.assertIsNone(self.p.portal_url(self.ctx, {"external_id": "sub_1"}, "https://x"))
        self.assertEqual(self.http.calls, [])


class InboundGateTests(SimpleTestCase):
    def setUp(self):
        self.p = StripeProvider()
        self.http = FakeHttp()
        self.ctx = make_ctx(self.http)

    def test_get_is_405(self):
        self.assertEqual(webhook(self.p, self.ctx, {}, method="GET").status, 405)

    def test_a_bad_signature_is_400_and_does_nothing(self):
        r = webhook(self.p, self.ctx, event("invoice.paid", {}), header=sign(b"{}", "whsec_wrong"))
        self.assertEqual((r.status, r.body, r.reports), (400, "invalid signature", ()))
        self.assertEqual(self.http.calls, [])

    def test_a_missing_header_is_400(self):
        r = self.p.inbound(self.ctx, InboundRequest("POST", {}, b"{}"))
        self.assertEqual(r.status, 400)

    def test_a_stale_signature_is_400(self):
        body = json.dumps(event("invoice.paid", {})).encode()
        r = webhook(self.p, self.ctx, body, header=sign(body, t=int(NOW.timestamp()) - 301))
        self.assertEqual(r.status, 400)

    def test_a_replayed_capture_after_the_window_is_400(self):
        body = json.dumps(event("customer.subscription.deleted", {"metadata": META})).encode()
        header = sign(body)
        self.assertEqual(webhook(self.p, self.ctx, body, header=header).status, 200)
        later = make_ctx(self.http, now=datetime.fromtimestamp(NOW.timestamp() + 3600, tz=timezone.utc))
        self.assertEqual(webhook(self.p, later, body, header=header).status, 400)

    def test_no_webhook_secret_is_a_secret_not_found(self):
        from osds.adapter_api import SecretNotFound

        ctx = make_ctx(self.http, secrets={"secret_key": SECRET_KEY})
        with self.assertRaises(SecretNotFound):  # core maps this to 503
            webhook(self.p, ctx, {})

    def test_malformed_bodies_signed_correctly_are_400(self):
        for body in (b"not json", b"\xff\xfe", b"[]", b'"x"', b"{}", b'{"id":"e","type":"t"}',
                     b'{"id":"e","type":"t","data":{"object":"x"}}'):
            with self.subTest(body=body):
                self.assertEqual(webhook(self.p, self.ctx, body).status, 400)

    def test_unhandled_events_are_acknowledged_with_no_reports(self):
        for t in ("customer.created", "invoice.payment_succeeded", "payment_intent.created"):
            with self.subTest(t=t):
                r = webhook(self.p, self.ctx, event(t, {"metadata": META}))
                self.assertEqual((r.status, r.reports), (200, ()))
        self.assertEqual(self.http.calls, [])

    def test_a_lookup_failure_is_503_so_stripe_retries(self):
        self.http.add("GET", "subscriptions/sub_1", {"error": {"type": "api_error"}}, status=500)
        session = {"mode": "subscription", "client_reference_id": "chk_1", "subscription": "sub_1",
                   "customer": "cus_1", "metadata": META}
        r = webhook(self.p, self.ctx, event("checkout.session.completed", session))
        self.assertEqual((r.status, r.reports), (503, ()))


class CheckoutCompletedTests(SimpleTestCase):
    def setUp(self):
        self.p, self.http = StripeProvider(), FakeHttp()
        self.ctx = make_ctx(self.http)
        self.session = {"mode": "subscription", "client_reference_id": "chk_1", "subscription": "sub_1",
                        "customer": "cus_1", "metadata": META}

    def run_it(self, **over):
        return webhook(self.p, self.ctx, event("checkout.session.completed", {**self.session, **over}))

    def test_active_subscription(self):
        self.http.add("GET", "subscriptions/sub_1", {"id": "sub_1", "status": "active",
                                                     "current_period_end": PERIOD_END})
        r = self.run_it()
        self.assertEqual(r.status, 200)
        (rep,) = r.reports
        self.assertEqual((rep.kind, rep.external_event_id, rep.checkout_attempt_id, rep.listing_id),
                         ("checkout_completed", "evt_1", "chk_1", "listing_1"))
        self.assertEqual(rep.period_end, DT)
        self.assertIsNone(rep.trial_ends_at)
        self.assertEqual(rep.payment_ref, {"adapter": "stripe", "external_id": "sub_1", "customer": "cus_1"})

    def test_period_end_on_the_items_in_the_newer_api_shape(self):
        self.http.add("GET", "subscriptions/sub_1", {"id": "sub_1", "status": "active", "items": {
            "data": [{"current_period_end": PERIOD_END}]}})
        self.assertEqual(self.run_it().reports[0].period_end, DT)

    def test_trial(self):
        self.http.add("GET", "subscriptions/sub_1", {"id": "sub_1", "status": "trialing",
                                                     "trial_end": PERIOD_END, "current_period_end": PERIOD_END})
        (rep,) = self.run_it().reports
        self.assertEqual(rep.trial_ends_at, DT)

    def test_a_subscription_without_a_period_end_is_retried(self):
        self.http.add("GET", "subscriptions/sub_1", {"id": "sub_1", "status": "active"})
        self.assertEqual(self.run_it().status, 503)

    def test_other_sessions_are_ignored(self):
        self.assertEqual(self.run_it(mode="payment").reports, ())
        self.assertEqual(self.run_it(client_reference_id=None, metadata={}).reports, ())
        self.assertEqual(self.run_it(subscription=None).reports, ())
        self.assertEqual(self.http.calls, [])

    def test_the_attempt_falls_back_to_metadata(self):
        self.http.add("GET", "subscriptions/sub_1", {"status": "active", "current_period_end": PERIOD_END})
        (rep,) = self.run_it(client_reference_id=None).reports
        self.assertEqual(rep.checkout_attempt_id, "chk_1")


class InvoiceTests(SimpleTestCase):
    def setUp(self):
        self.p, self.http = StripeProvider(), FakeHttp()
        self.ctx = make_ctx(self.http)

    def invoice(self, **over):
        base = {"id": "in_1", "subscription": "sub_1", "customer": "cus_1", "amount_paid": 4900,
                "currency": "usd", "billing_reason": "subscription_cycle",
                "subscription_details": {"metadata": META},
                "lines": {"data": [{"period": {"start": PERIOD_END - 100, "end": PERIOD_END}}]}}
        base.update(over)
        return base

    def test_paid(self):
        (rep,) = webhook(self.p, self.ctx, event("invoice.paid", self.invoice())).reports
        self.assertEqual((rep.kind, rep.amount_minor, rep.currency, rep.invoice_ref),
                         ("payment_succeeded", 4900, "USD", "in_1"))
        self.assertEqual((rep.period_end, rep.listing_id, rep.checkout_attempt_id), (DT, "listing_1", "chk_1"))
        self.assertEqual(self.http.calls, [])  # metadata was on the invoice

    def test_the_newer_api_shape(self):
        inv = self.invoice(subscription=None, subscription_details=None)
        inv["parent"] = {"subscription_details": {"subscription": "sub_1", "metadata": META}}
        (rep,) = webhook(self.p, self.ctx, event("invoice.paid", inv)).reports
        self.assertEqual((rep.listing_id, rep.payment_ref["external_id"]), ("listing_1", "sub_1"))

    def test_metadata_is_fetched_from_the_subscription_when_the_invoice_lacks_it(self):
        self.http.add("GET", "subscriptions/sub_1", {"id": "sub_1", "metadata": META})
        (rep,) = webhook(self.p, self.ctx, event("invoice.paid", self.invoice(subscription_details=None))).reports
        self.assertEqual(rep.listing_id, "listing_1")

    def test_a_zero_dollar_trial_invoice_is_skipped(self):
        inv = self.invoice(amount_paid=0, billing_reason="subscription_create")
        self.assertEqual(webhook(self.p, self.ctx, event("invoice.paid", inv)).reports, ())

    def test_a_zero_dollar_renewal_is_not_skipped(self):
        inv = self.invoice(amount_paid=0, billing_reason="subscription_cycle")
        self.assertEqual(len(webhook(self.p, self.ctx, event("invoice.paid", inv)).reports), 1)

    def test_an_invoice_that_is_not_ours_is_ignored(self):
        inv = self.invoice(subscription_details={"metadata": {}})
        self.http.add("GET", "subscriptions/sub_1", {"id": "sub_1", "metadata": {}})
        self.assertEqual(webhook(self.p, self.ctx, event("invoice.paid", inv)).reports, ())
        self.assertEqual(webhook(self.p, self.ctx, event("invoice.paid", self.invoice(
            subscription=None, subscription_details=None))).reports, ())

    def test_a_vanished_subscription_is_ignored_not_retried(self):
        inv = self.invoice(subscription_details=None)  # the fake answers 404 resource_missing
        r = webhook(self.p, self.ctx, event("invoice.paid", inv))
        self.assertEqual((r.status, r.reports), (200, ()))

    def test_failed(self):
        inv = self.invoice(attempt_count=2, next_payment_attempt=PERIOD_END)
        (rep,) = webhook(self.p, self.ctx, event("invoice.payment_failed", inv)).reports
        self.assertEqual((rep.kind, rep.attempt, rep.next_retry_at, rep.failure_code),
                         ("payment_failed", 2, DT, "invoice_payment_failed"))
        inv = self.invoice(attempt_count=4, next_payment_attempt=None)
        (rep,) = webhook(self.p, self.ctx, event("invoice.payment_failed", inv, id_="evt_2")).reports
        self.assertIsNone(rep.next_retry_at)


class SubscriptionEventTests(SimpleTestCase):
    def setUp(self):
        self.p, self.http = StripeProvider(), FakeHttp()
        self.ctx = make_ctx(self.http)

    def sub(self, **over):
        base = {"id": "sub_1", "customer": "cus_1", "metadata": META, "current_period_end": PERIOD_END,
                "cancel_at_period_end": False,
                "items": {"data": [{"price": {"id": "price_FEAT"}}]}}
        base.update(over)
        return base

    def updated(self, sub, previous, id_="evt_u"):
        return webhook(self.p, self.ctx, event("customer.subscription.updated", sub, id_=id_,
                                               previous_attributes=previous))

    def test_cancel_at_period_end_on(self):
        (rep,) = self.updated(self.sub(cancel_at_period_end=True,
                                       cancellation_details={"reason": "cancellation_requested"}),
                              {"cancel_at_period_end": False}).reports
        self.assertEqual((rep.kind, rep.at_period_end, rep.canceled_by, rep.reason),
                         ("subscription_canceled", True, "owner", "cancellation_requested"))

    def test_cancel_at_period_end_switched_off_is_a_payment_succeeded(self):
        (rep,) = self.updated(self.sub(), {"cancel_at_period_end": True}).reports
        self.assertEqual((rep.kind, rep.period_end), ("payment_succeeded", DT))

    def test_a_price_change_to_a_configured_tier(self):
        sub = self.sub(items={"data": [{"price": {"id": "price_VERI"}}]})
        (rep,) = self.updated(sub, {"items": {"data": []}}).reports
        self.assertEqual((rep.kind, rep.tier_key, rep.period_end), ("subscription_changed", "verified", DT))

    def test_a_price_change_to_an_unknown_price_is_ignored(self):
        sub = self.sub(items={"data": [{"price": {"id": "price_OTHER"}}]})
        self.assertEqual(self.updated(sub, {"items": {}}).reports, ())

    def test_other_updates_are_ignored(self):
        self.assertEqual(self.updated(self.sub(), {"default_payment_method": None}).reports, ())
        self.assertEqual(self.updated(self.sub(), {}).reports, ())

    def test_not_ours_is_ignored(self):
        self.assertEqual(self.updated(self.sub(metadata={}), {"cancel_at_period_end": False}).reports, ())

    def test_deleted(self):
        (rep,) = webhook(self.p, self.ctx, event("customer.subscription.deleted", self.sub(
            cancellation_details={"reason": "payment_failed"}))).reports
        self.assertEqual((rep.kind, rep.at_period_end, rep.canceled_by, rep.reason),
                         ("subscription_canceled", False, "provider", "payment_failed"))

    def test_deleted_not_ours(self):
        self.assertEqual(webhook(self.p, self.ctx, event(
            "customer.subscription.deleted", self.sub(metadata={}))).reports, ())


class RefundTests(SimpleTestCase):
    def setUp(self):
        self.p, self.http = StripeProvider(), FakeHttp()
        self.ctx = make_ctx(self.http)
        self.charge = {"id": "ch_1", "customer": "cus_1", "refunded": True, "amount_refunded": 4900,
                       "currency": "usd"}

    def test_a_full_refund(self):
        self.http.add("GET", "subscriptions", {"data": [{"id": "sub_9", "metadata": {}},
                                                       {"id": "sub_1", "metadata": META}]})
        (rep,) = webhook(self.p, self.ctx, event("charge.refunded", self.charge)).reports
        self.assertEqual((rep.kind, rep.amount_minor, rep.currency, rep.listing_id),
                         ("refunded", 4900, "USD", "listing_1"))
        self.assertEqual(rep.payment_ref["external_id"], "sub_1")
        url = self.http.calls[-1]["url"]
        self.assertIn("customer=cus_1", url)
        self.assertIn("status=all", url)

    def test_a_partial_refund_is_ignored(self):
        r = webhook(self.p, self.ctx, event("charge.refunded", {**self.charge, "refunded": False}))
        self.assertEqual(r.reports, ())
        self.assertEqual(self.http.calls, [])

    def test_a_charge_without_a_customer_or_one_of_ours_is_ignored(self):
        self.assertEqual(webhook(self.p, self.ctx, event("charge.refunded", {**self.charge, "customer": None})).reports, ())
        self.http.add("GET", "subscriptions", {"data": [{"id": "sub_9", "metadata": {}}]})
        self.assertEqual(webhook(self.p, self.ctx, event("charge.refunded", self.charge)).reports, ())

    def test_a_lookup_failure_is_retried(self):
        self.http.add("GET", "subscriptions", {"error": {"type": "api_error"}}, status=500)
        self.assertEqual(webhook(self.p, self.ctx, event("charge.refunded", self.charge)).status, 503)
