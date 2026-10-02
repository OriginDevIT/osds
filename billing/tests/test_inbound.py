"""The inbound route a payment provider calls (decisions.md §4.11): CSRF-exempt,
tenant-resolved, signature-checked by the adapter, idempotent."""

from __future__ import annotations

import json
from datetime import timedelta

from django.test import Client, override_settings
from django.utils import timezone

from audit.models import CommandLog
from billing.models import CheckoutAttempt, Entitlement, PaymentReceipt
from billing.tests.base import DAY, T0
from billing.tests.payments_base import ADAPTER_ID, WEBHOOK_SECRET, PaymentTestCase, sign
from osds.adapters import override_capability
from tenants.models import InstallSetup, Tenant
from tenants.secrets import set_secret

S = Entitlement.Status
URL = f"/_adapters/{ADAPTER_ID}/inbound/"


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class InboundTests(PaymentTestCase):
    def setUp(self):
        super().setUp()
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        set_secret("stubpay_webhook_secret", WEBHOOK_SECRET, tenant=self.tenant)
        self.client = Client(enforce_csrf_checks=True)  # the route must not need a token
        self.attempt = CheckoutAttempt.all_tenants.create(
            tenant=self.tenant, listing=self.listing, tier=self.featured,
            adapter_id=ADAPTER_ID, expires_at=timezone.now() + DAY,
        )

    def body(self, *events) -> bytes:
        return json.dumps({"events": list(events)}).encode()

    def completed(self, event_id="evt_1"):
        return {"kind": "checkout_completed", "id": event_id, "attempt": self.attempt.public_id,
                "period_end": (timezone.now() + 30 * DAY).isoformat(), "sub": "sub_9"}

    def post(self, body: bytes, *, signature=None, host="acme.test", client=None, **extra):
        signature = sign(body) if signature is None else signature
        return (client or self.client).post(
            URL, data=body, content_type="application/json", HTTP_HOST=host,
            HTTP_X_STUB_SIGNATURE=signature, **extra,
        )

    def test_a_signed_event_is_applied(self):
        r = self.post(self.body(self.completed()))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"received": 1, "applied": 1, "replayed": 0, "ignored": 0})
        ent = Entitlement.all_tenants.get()
        self.assertEqual((ent.status, ent.payment_ref["external_id"]), (S.ACTIVE, "sub_9"))
        self.assertEqual(self.fresh().current_tier, self.featured)

    def test_the_route_needs_no_csrf_token(self):
        self.assertEqual(self.post(self.body(self.completed())).status_code, 200)

    def test_a_bad_or_missing_signature_is_refused_and_applies_nothing(self):
        body = self.body(self.completed())
        for signature in ("0" * 64, "", sign(body, "wrong-secret")):
            with self.subTest(signature=signature[:8]):
                r = self.post(body, signature=signature)
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.content, b"bad signature")
        self.assertFalse(Entitlement.all_tenants.exists())
        self.assertFalse(PaymentReceipt.all_tenants.exists())

    def test_a_body_changed_after_signing_is_refused(self):
        body = self.body(self.completed())
        r = self.post(body.replace(b"sub_9", b"sub_8"), signature=sign(body))
        self.assertEqual(r.status_code, 400)

    def test_a_redelivery_is_acknowledged_and_applies_once(self):
        body = self.body(self.completed())
        self.post(body)
        r = self.post(body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["replayed"], 1)
        self.assertEqual(Entitlement.all_tenants.count(), 1)
        self.assertEqual(PaymentReceipt.all_tenants.count(), 1)

    def test_an_event_core_cannot_apply_is_still_acknowledged(self):
        r = self.post(self.body({"kind": "payment_failed", "id": "evt_x", "listing": "listing_NOPE"}))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ignored"], 1)

    def test_several_events_in_one_delivery(self):
        r = self.post(self.body(
            self.completed("evt_a"),
            {"kind": "payment_succeeded", "id": "evt_b", "listing": self.listing.public_id,
             "period_end": (timezone.now() + 60 * DAY).isoformat()},
        ))
        self.assertEqual(r.json()["applied"], 2)

    def test_only_post_is_allowed(self):
        self.assertEqual(self.client.get(URL, HTTP_HOST="acme.test").status_code, 405)

    def test_the_route_exists_only_for_a_registered_adapter_on_a_tenant_host(self):
        body = self.body(self.completed())
        self.assertEqual(self.client.post("/_adapters/nobody/inbound/", data=body, content_type="application/json",
                                          HTTP_HOST="acme.test").status_code, 404)
        self.assertEqual(self.post(body, host="console.test").status_code, 404)
        with override_capability({}):
            self.assertEqual(self.post(body).status_code, 404)

    def test_an_unconfigured_secret_is_a_503_so_the_provider_retries(self):
        from tenants.models import Secret

        Secret.objects.filter(key="stubpay_webhook_secret").delete()
        self.assertEqual(self.post(self.body(self.completed())).status_code, 503)
        self.assertFalse(Entitlement.all_tenants.exists())

    def test_an_oversize_body_is_refused_before_the_adapter_sees_it(self):
        big = b"x" * (256 * 1024 + 1)
        self.assertEqual(self.post(big).status_code, 413)

    def test_the_tenant_is_the_hosts_so_another_directorys_secret_is_not_used(self):
        other = Tenant.objects.create(slug="other", name="Other", primary_domain="other.test",
                                      domain_verified_at=timezone.now())
        set_secret("stubpay_webhook_secret", "other-secret", tenant=other)
        body = self.body(self.completed())
        self.assertEqual(self.post(body, host="other.test").status_code, 400)  # signed with acme's
        self.assertEqual(self.post(body, host="acme.test").status_code, 200)

    def test_a_flood_from_one_address_is_rate_limited(self):
        body = self.body(self.completed())
        for _ in range(300):
            self.post(body)
        r = self.post(body)
        self.assertEqual(r.status_code, 429)
        self.assertGreater(int(r["Retry-After"]), 0)

    def test_the_log_row_names_the_adapter_and_the_event(self):
        self.post(self.body(self.completed("evt_77")))
        row = CommandLog.objects.get(command="entitlement.reportPayment")
        self.assertEqual((row.idempotency_key, row.adapter_id), (f"{ADAPTER_ID}:evt_77", ADAPTER_ID))
        self.assertEqual(row.outcome, "applied")
