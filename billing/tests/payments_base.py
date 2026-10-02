"""A stub payment provider and an owner fixture for the payment tests.

``StubProvider`` follows the provider contract in ``osds.adapter_api`` and is
registered under both payment capabilities by ``payments_stub()``. Its inbound
route verifies an HMAC signature over the raw body with a webhook secret read
through the context, so the signature path core hands the adapter is exercised
end to end without a vendor.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
from datetime import datetime, timedelta

from django.test import Client
from django.utils import timezone

from billing.tests.base import BillingTransactionTestCase
from directory import owner_auth
from directory.models import DirectoryUser, Listing, OwnerSession
from osds.adapter_api import (
    CheckoutResult,
    InboundResult,
    PaymentError,
    PaymentReport,
    SettingField,
)
from osds.adapters import override_capability

ADAPTER_ID = "stubpay"
WEBHOOK_SECRET = "whsec-test"


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _dt(value):
    return datetime.fromisoformat(value) if value else None


class StubProvider:
    adapter_id = ADAPTER_ID
    egress_allowlist = ("api.stubpay.test",)

    def __init__(self, available: bool = True):
        self._available = available
        self.checkouts: list = []
        self.cancels: list = []
        self.portals: list = []
        self.fail_checkout = False
        self.fail_cancel = False
        self.portal = "https://stubpay.test/portal/abc"

    def available(self, ctx) -> bool:
        return self._available

    def settings_fields(self):
        return [
            SettingField("account", "Account id", help_text="Your account."),
            SettingField("api_key", "API key", secret=True, required=True),
            SettingField("webhook_secret", "Webhook secret", secret=True),
        ]

    def create_checkout(self, ctx, request):
        self.checkouts.append(request)
        if self.fail_checkout:
            raise PaymentError("boom")
        return CheckoutResult(
            url=f"https://stubpay.test/pay/{request.attempt_id}", external_ref=f"cs_{request.attempt_id}"
        )

    def cancel_subscription(self, ctx, payment_ref, *, at_period_end):
        self.cancels.append((dict(payment_ref), at_period_end))
        if self.fail_cancel:
            raise PaymentError("boom")

    def portal_url(self, ctx, payment_ref, return_url):
        self.portals.append((dict(payment_ref), return_url))
        return self.portal

    def inbound(self, ctx, request):
        if request.method != "POST":
            return InboundResult(405, "method")
        presented = request.headers.get("x-stub-signature", "")
        expected = sign(request.body, ctx.secret("webhook_secret"))
        if not hmac.compare_digest(presented, expected):
            return InboundResult(400, "bad signature")
        reports = []
        for e in json.loads(request.body)["events"]:
            reports.append(
                PaymentReport(
                    kind=e["kind"], external_event_id=e["id"],
                    checkout_attempt_id=e.get("attempt", ""), listing_id=e.get("listing", ""),
                    tier_key=e.get("tier", ""), period_end=_dt(e.get("period_end")),
                    trial_ends_at=_dt(e.get("trial_ends_at")),
                    payment_ref={"adapter": ADAPTER_ID, "external_id": e.get("sub", "sub_1")},
                    amount_minor=e.get("amount"), currency=e.get("currency", ""),
                    invoice_ref=e.get("invoice", ""), failure_code=e.get("failure_code", ""),
                    attempt=e.get("n"), at_period_end=e.get("at_period_end", True),
                    canceled_by=e.get("canceled_by", ""), reason=e.get("reason", ""),
                )
            )
        return InboundResult(200, "ok", tuple(reports))


@contextlib.contextmanager
def payments_stub(available: bool = True):
    provider = StubProvider(available)
    with override_capability({"payments.checkout": provider, "payments.subscription": provider}):
        yield provider


class PaymentFixtures:
    """Mixed into ``BillingTransactionTestCase``: the stub, an owner of the
    fixture listing, and a signed-in owner client."""

    def setUp(self):
        super().setUp()
        self.provider = StubProvider()
        ctx = override_capability(
            {"payments.checkout": self.provider, "payments.subscription": self.provider}
        )
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.owner = DirectoryUser.all_tenants.create(
            tenant=self.tenant, email="owner@acme-co.example", name="Olive Owner"
        )
        Listing.all_tenants.filter(pk=self.listing.pk).update(
            owner=self.owner, status=Listing.Status.CLAIMED
        )
        self.listing = Listing.all_tenants.get(pk=self.listing.pk)

    def owner_client(self, user=None) -> Client:
        user = user or self.owner
        secret = f"cookie-{user.pk}"
        OwnerSession.all_tenants.create(
            tenant=self.tenant, user=user, token_hash=owner_auth._digest(secret),
            expires_at=timezone.now() + timedelta(days=1),
        )
        c = Client()
        c.cookies[owner_auth.COOKIE_NAME] = secret
        return c


class PaymentTestCase(PaymentFixtures, BillingTransactionTestCase):
    pass
