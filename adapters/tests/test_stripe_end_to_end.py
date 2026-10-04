"""Stripe webhooks through real core: ``/_adapters/stripe/inbound/`` with the
real context builder, secret store and entitlement service. Only the socket is
faked (the opener behind ``ctx.http``), so the egress allowlist is exercised too."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest import mock

from django.test import Client, override_settings
from django.utils import timezone

from adapters.stripe.provider import StripeProvider
from billing.models import CheckoutAttempt, Entitlement, PaymentReceipt
from billing.tests.base import DAY
from billing.tests.payments_base import PaymentTestCase
from osds.adapters import override_capability
from tenants.models import InstallSetup, Tenant
from tenants.secrets import set_secret

S = Entitlement.Status
URL = "/_adapters/stripe/inbound/"
WHSEC = "whsec_E2E123"


def stripe_signature(body: bytes, secret=WHSEC, t=None) -> str:
    t = int(time.time()) if t is None else t
    return f"t={t},v1=" + hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


class FakeResponse:
    def __init__(self, status, payload):
        self.status, self._body, self.headers = status, json.dumps(payload).encode(), {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def open(self, request, timeout=None):
        self.requests.append(request)
        path = request.full_url.split("/v1/", 1)[1].split("?", 1)[0]
        status, payload = self.routes.get((request.get_method(), path), (404, {"error": {
            "type": "invalid_request_error", "code": "resource_missing"}}))
        return FakeResponse(status, payload)


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class StripeEndToEndTests(PaymentTestCase):
    def setUp(self):
        super().setUp()
        ctx = override_capability({"payments.checkout": StripeProvider(), "payments.subscription": StripeProvider()})
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        set_secret("stripe_secret_key", "sk_test_E2E", tenant=self.tenant)
        set_secret("stripe_webhook_secret", WHSEC, tenant=self.tenant)
        settings = dict(self.tenant.settings or {})
        settings["adapters"] = {"stripe": {"price_ids": "featured=price_FEAT"}}
        Tenant.objects.filter(pk=self.tenant.pk).update(settings=settings)
        self.client = Client(enforce_csrf_checks=True)
        self.attempt = CheckoutAttempt.all_tenants.create(
            tenant=self.tenant, listing=self.listing, tier=self.featured,
            adapter_id="stripe", expires_at=timezone.now() + DAY,
        )
        self.period_end = int(time.time()) + 30 * 86400
        self.opener = FakeOpener({("GET", "subscriptions/sub_1"): (200, {
            "id": "sub_1", "status": "active", "current_period_end": self.period_end})})
        patcher = mock.patch("osds.adapter_context.urllib.request.build_opener", return_value=self.opener)
        patcher.start()
        self.addCleanup(patcher.stop)

    def completed(self, event_id="evt_1"):
        return json.dumps({"id": event_id, "type": "checkout.session.completed", "data": {"object": {
            "mode": "subscription", "client_reference_id": self.attempt.public_id,
            "subscription": "sub_1", "customer": "cus_1",
            "metadata": {"attempt_id": self.attempt.public_id, "listing_id": self.listing.public_id,
                         "tier": "featured"}}}}).encode()

    def post(self, body, header=None):
        header = stripe_signature(body) if header is None else header
        return self.client.post(URL, data=body, content_type="application/json",
                                HTTP_HOST="acme.test", HTTP_STRIPE_SIGNATURE=header)

    def test_a_signed_checkout_completed_activates_the_listing(self):
        r = self.post(self.completed())
        self.assertEqual(r.status_code, 200, r.content)
        ent = Entitlement.all_tenants.get()
        self.assertEqual((ent.status, ent.payment_ref["external_id"], ent.payment_ref["customer"]),
                         (S.ACTIVE, "sub_1", "cus_1"))
        self.assertEqual(self.fresh().current_tier, self.featured)
        self.assertEqual([q.full_url.split("?")[0] for q in self.opener.requests],
                         ["https://api.stripe.com/v1/subscriptions/sub_1"])
        self.assertEqual(self.opener.requests[0].get_header("Authorization"), "Bearer sk_test_E2E")

    def test_a_replayed_delivery_applies_once(self):
        body = self.completed()
        header = stripe_signature(body)
        self.assertEqual(self.post(body, header).status_code, 200)
        r = self.post(body, header)
        self.assertEqual((r.status_code, r.json()["replayed"]), (200, 1))
        self.assertEqual(Entitlement.all_tenants.count(), 1)
        self.assertEqual(PaymentReceipt.all_tenants.count(), 1)

    def test_a_redelivery_with_a_new_signature_is_still_applied_once(self):
        body = self.completed()
        self.post(body)
        r = self.post(body, stripe_signature(body, t=int(time.time()) + 5))
        self.assertEqual(r.json()["replayed"], 1)
        self.assertEqual(PaymentReceipt.all_tenants.count(), 1)

    def test_a_wrong_secret_applies_nothing(self):
        body = self.completed()
        self.assertEqual(self.post(body, stripe_signature(body, "whsec_WRONG")).status_code, 400)
        self.assertFalse(Entitlement.all_tenants.exists())
        self.assertEqual(self.opener.requests, [])  # nothing is looked up for an unverified body

    def test_a_stale_capture_applies_nothing(self):
        body = self.completed()
        self.assertEqual(self.post(body, stripe_signature(body, t=int(time.time()) - 600)).status_code, 400)
        self.assertFalse(Entitlement.all_tenants.exists())

    def test_a_missing_or_malformed_header_applies_nothing(self):
        body = self.completed()
        for header in ("", "garbage", "t=1"):
            with self.subTest(header=header):
                self.assertEqual(self.post(body, header).status_code, 400)
        self.assertFalse(Entitlement.all_tenants.exists())

    def test_a_signed_malformed_body_is_400(self):
        self.assertEqual(self.post(b"not json").status_code, 400)

    def test_an_unconfigured_webhook_secret_is_503(self):
        from tenants.models import Secret

        Secret.objects.filter(key="stripe_webhook_secret").delete()
        self.assertEqual(self.post(self.completed()).status_code, 503)

    def test_a_stripe_lookup_failure_is_503_and_nothing_is_recorded(self):
        self.opener.routes[("GET", "subscriptions/sub_1")] = (500, {"error": {"type": "api_error"}})
        self.assertEqual(self.post(self.completed()).status_code, 503)
        self.assertFalse(PaymentReceipt.all_tenants.exists())
        self.opener.routes[("GET", "subscriptions/sub_1")] = (200, {
            "status": "active", "current_period_end": self.period_end})
        self.assertEqual(self.post(self.completed()).status_code, 200)  # Stripe's retry lands
        self.assertEqual(Entitlement.all_tenants.get().status, S.ACTIVE)

    def test_the_route_needs_no_csrf_token(self):
        self.assertEqual(self.post(self.completed()).status_code, 200)

    def test_the_context_reaches_only_the_stripe_api(self):
        from osds.adapter_api import EgressDenied
        from osds.adapter_context import build_context

        ctx = build_context(self.tenant, StripeProvider())
        for url in ("https://evil.example/v1/x", "http://api.stripe.com/v1/x", "https://api.stripe.com.evil.test/"):
            with self.subTest(url=url), self.assertRaises(EgressDenied):
                ctx.http("GET", url)
