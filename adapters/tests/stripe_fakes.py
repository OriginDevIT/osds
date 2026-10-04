"""Fakes for the Stripe adapter tests: an ``AdapterContext`` whose HTTP is a
script, so no test touches the network or imports core."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

from osds.adapter_api import AdapterContext, HttpResponse, SecretNotFound

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
SECRET_KEY = "sk_test_ABC123"
WEBHOOK_SECRET = "whsec_ABC123"
CONFIG = {"price_ids": "featured=price_FEAT, verified=price_VERI"}


def sign(body: bytes, secret: str = WEBHOOK_SECRET, *, t: "int | None" = None) -> str:
    """A ``Stripe-Signature`` header built straight from Stripe's documented
    scheme, independently of ``adapters.stripe.signature``."""
    t = int(NOW.timestamp()) if t is None else t
    digest = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


class FakeHttp:
    """Scripted responses by ``(method, path-prefix)``; records every call."""

    def __init__(self):
        self.routes: dict = {}
        self.calls: list = []

    def add(self, method, path, body, status=200):
        self.routes[(method, path)] = (status, body)

    def __call__(self, method, url, *, headers=None, body=None, timeout=30):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "body": body, "timeout": timeout})
        path = url.split("api.stripe.com/v1/", 1)[1].split("?", 1)[0] if "api.stripe.com/v1/" in url else url
        for (m, prefix), (status, payload) in self.routes.items():
            if m == method and path.startswith(prefix):
                return HttpResponse(status, json.dumps(payload).encode(), {})
        return HttpResponse(404, json.dumps({"error": {"type": "invalid_request_error",
                                                       "code": "resource_missing"}}).encode(), {})


def make_ctx(http=None, *, config=None, secrets=None, now=NOW):
    secrets = {"secret_key": SECRET_KEY, "webhook_secret": WEBHOOK_SECRET} if secrets is None else secrets

    def secret(key):
        try:
            return secrets[key]
        except KeyError:
            raise SecretNotFound(key) from None

    return AdapterContext(
        tenant_id="tnt_X", tenant_slug="acme", tenant_domain="acme.test",
        config=dict(CONFIG if config is None else config), secret=secret,
        http=http or FakeHttp(), clock=lambda: now,
    )
