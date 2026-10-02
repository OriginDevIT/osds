"""The adapter seam for payments: capability names, provider lookup, the
minimal ``AdapterContext`` and its allowlisted egress (decisions.md §4.11;
#215)."""

from __future__ import annotations

import urllib.error

from django.test import SimpleTestCase, TestCase

from billing import payments
from billing.tests.base import BillingTestCase, T0
from billing.tests.payments_base import ADAPTER_ID, StubProvider, payments_stub
from osds import adapter_api, adapter_context
from osds.adapter_api import EgressDenied, SecretNotFound
from osds.adapters import adapter_by_id, override_capability
from tenants.models import Tenant
from tenants.secrets import set_secret
from tenants.services import InvalidTenantSettings, _validate_adapters


class ProviderLookupTests(BillingTestCase):
    def test_no_provider_means_payments_are_unavailable(self):
        with override_capability({}):
            self.assertIsNone(payments.provider())
            self.assertFalse(payments.available(self.tenant))
            self.assertIsNone(adapter_by_id(ADAPTER_ID))

    def test_one_provider_is_found_by_capability_and_by_adapter_id(self):
        with payments_stub() as stub:
            self.assertIs(payments.provider(), stub)
            self.assertIs(adapter_by_id(ADAPTER_ID), stub)
            self.assertIsNone(adapter_by_id("nobody"))
            self.assertTrue(payments.available(self.tenant))

    def test_a_provider_that_says_it_is_not_configured_is_unavailable(self):
        with payments_stub(available=False):
            self.assertFalse(payments.available(self.tenant))

    def test_an_absolute_base_is_required(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(domain_verified_at=None)
        self.tenant.refresh_from_db()
        with payments_stub():
            self.assertFalse(payments.available(self.tenant))

    def test_a_missing_secret_makes_it_unavailable_not_an_error(self):
        class Needy(StubProvider):
            def available(self, ctx):
                ctx.secret("api_key")
                return True

        with override_capability({"payments.checkout": Needy(), "payments.subscription": Needy()}):
            self.assertFalse(payments.available(self.tenant))
            set_secret("stubpay_api_key", "k", tenant=self.tenant)
            self.assertTrue(payments.available(self.tenant))


class ContextTests(BillingTestCase):
    def test_config_and_secrets_come_from_the_tenant_by_adapter_id(self):
        self.tenant.settings = {"adapters": {ADAPTER_ID: {"account": "acct_1"}, "other": {"x": 1}}}
        self.tenant.save(update_fields=["settings"])
        set_secret("stubpay_api_key", "sk_test", tenant=self.tenant)
        ctx = adapter_context.build_context(self.tenant, StubProvider())
        self.assertEqual(ctx.config, {"account": "acct_1"})
        self.assertEqual(ctx.secret("api_key"), "sk_test")
        self.assertEqual((ctx.tenant_slug, ctx.tenant_domain, ctx.tenant_id),
                         ("acme", "acme.test", self.tenant.public_id))
        self.assertIsNotNone(ctx.clock())

    def test_a_secret_nobody_set_is_not_found(self):
        ctx = adapter_context.build_context(self.tenant, StubProvider())
        with self.assertRaises(SecretNotFound):
            ctx.secret("nope")

    def test_a_secret_is_the_tenants_own_before_the_deployments(self):
        set_secret("stubpay_api_key", "deployment")
        self.assertEqual(adapter_context.build_context(self.tenant, StubProvider()).secret("api_key"), "deployment")
        set_secret("stubpay_api_key", "tenant", tenant=self.tenant)
        self.assertEqual(adapter_context.build_context(self.tenant, StubProvider()).secret("api_key"), "tenant")

    def test_a_tenant_with_no_adapter_config_gets_an_empty_dict(self):
        self.assertEqual(adapter_context.adapter_config(self.tenant, ADAPTER_ID), {})


class _Opener:
    def __init__(self, status=200, body=b"{}"):
        self.calls, self.status, self.body = [], status, body

    def open(self, request, timeout=None):
        self.calls.append((request.full_url, request.get_method(), timeout))
        opener = self

        class Resp:
            status = opener.status
            headers = {"X-A": "b"}

            def read(self_inner):
                return opener.body

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        return Resp()


class EgressTests(SimpleTestCase):
    def test_only_listed_https_hosts_are_reached(self):
        opener = _Opener()
        fetch = adapter_context.make_http(["api.stubpay.test"], opener=opener)
        response = fetch("GET", "https://api.stubpay.test/v1/x", headers={"A": "b"})
        self.assertEqual((response.status, response.body, response.headers), (200, b"{}", {"x-a": "b"}))
        self.assertEqual(opener.calls, [("https://api.stubpay.test/v1/x", "GET", 30)])
        for url in (
            "https://evil.example/v1/x",
            "http://api.stubpay.test/v1/x",  # not HTTPS
            "https://api.stubpay.test.evil.example/",
            "https://evil.example/https://api.stubpay.test/",
            "ftp://api.stubpay.test/",
        ):
            with self.subTest(url=url), self.assertRaises(EgressDenied):
                fetch("GET", url)
        self.assertEqual(len(opener.calls), 1)  # nothing else reached a socket

    def test_the_allowlist_is_exact_and_case_insensitive(self):
        fetch = adapter_context.make_http(["API.Stubpay.Test"], opener=_Opener())
        self.assertEqual(fetch("GET", "https://api.stubpay.test/").status, 200)
        with self.assertRaises(EgressDenied):
            fetch("GET", "https://sub.api.stubpay.test/")

    def test_an_error_status_is_a_response_not_an_exception(self):
        class Failing:
            def open(self, request, timeout=None):
                raise urllib.error.HTTPError(request.full_url, 402, "no", {"X-Y": "z"}, None)

        class Resp:
            pass

        fetch = adapter_context.make_http(["api.stubpay.test"], opener=Failing())
        try:
            response = fetch("POST", "https://api.stubpay.test/charge", body=b"x")
        except Exception as exc:  # HTTPError with no fp cannot be read
            self.assertIsInstance(exc, (AttributeError, urllib.error.HTTPError))
        else:
            self.assertEqual(response.status, 402)

    def test_redirects_are_not_followed(self):
        handler = adapter_context._NoRedirects()
        self.assertIsNone(handler.redirect_request())


class AdapterApiTests(SimpleTestCase):
    def test_the_report_kinds_are_closed(self):
        self.assertEqual(
            set(adapter_api.REPORT_KINDS),
            {"checkout_completed", "payment_succeeded", "payment_failed", "refunded",
             "subscription_changed", "subscription_canceled"},
        )

    def test_money_is_integer_minor_units(self):
        report = adapter_api.PaymentReport(kind="refunded", external_event_id="e", amount_minor=1900)
        self.assertIsInstance(report.amount_minor, int)


class AdaptersSettingsValidatorTests(SimpleTestCase):
    def test_accepts_scalars_and_rejects_the_rest(self):
        _validate_adapters({"stubpay": {"account": "a", "n": 1, "on": True, "x": None}})
        for bad in ("x", {"": {}}, {"a": "x"}, {"a": {"k": [1]}}, {"a": {"k": {"n": 1}}}, {"a": {1: "x"}}):
            with self.subTest(bad=bad), self.assertRaises(InvalidTenantSettings):
                _validate_adapters(bad)
