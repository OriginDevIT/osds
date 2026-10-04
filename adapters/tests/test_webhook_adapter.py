"""The webhook adapter (decisions.md §4.12), against a fake context: no network,
no database. What it asks for, how it signs, and how it reads the answer.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from adapters.webhook import signing
from adapters.webhook.adapter import (
    MAX_BODY_BYTES, WebhookAdapter, _retry_after_ms, parse_patterns,
)
from osds.adapter_api import EgressDenied, HttpResponse, SecretNotFound
from osds.adapter_logging import make_adapter_logger

URL = "https://hooks.example.test/in/T0K3N-abc123"
KEY = "whsec_" + "cd" * 32
NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)

ENVELOPE = {
    "id": "01TESTEVENT", "type": "lead.captured", "version": 1, "subject": "lead_1",
    "occurred_at": "2026-10-03T12:00:00.000Z", "actor": {"type": "visitor", "id": ""},
    "origin": None, "trace_id": "01TESTEVENT", "data": {"lead": {"id": "lead_1", "email": None}},
    "redacted": ["lead.email"], "tenant": {"id": "tnt_1", "slug": "acme", "domain": "acme.test"},
}


class FakeHttp:
    def __init__(self, response=None, error=None):
        self.response = response or HttpResponse(200, b"ok")
        self.error = error
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body, "timeout": timeout})
        if self.error:
            raise self.error
        return self.response


def ctx(*, config=None, http=None, secrets=None):
    secrets = {"url": URL, "signing_secret": KEY} if secrets is None else secrets
    resolved = set()

    def secret(name):
        if name not in secrets:
            raise SecretNotFound(name)
        resolved.add(secrets[name])
        return secrets[name]

    return SimpleNamespace(
        tenant_id="tnt_1", tenant_slug="acme", tenant_domain="acme.test",
        config={"enabled": True, "events": "*", "host": "hooks.example.test", **(config or {})},
        secret=secret, http=http or FakeHttp(), clock=lambda: NOW,
        logger=make_adapter_logger("webhook", resolved),
    )


adapter = WebhookAdapter()


class PatternTests(SimpleTestCase):
    def test_parsing(self):
        self.assertEqual(parse_patterns("Claim.*, lead.captured\n  *"), ["claim.*", "lead.captured", "*"])
        self.assertEqual(parse_patterns(""), [])
        self.assertEqual(parse_patterns(None), [])


class WantsTests(SimpleTestCase):
    def test_wants(self):
        cases = [
            ({"enabled": True, "events": "*"}, "lead.captured", True),
            ({"enabled": True, "events": "claim.*"}, "claim.submitted", True),
            ({"enabled": True, "events": "claim.*"}, "lead.captured", False),
            ({"enabled": True, "events": ""}, "lead.captured", False),
            ({"enabled": False, "events": "*"}, "lead.captured", False),
            ({"events": "*"}, "lead.captured", False),
            ({"enabled": "true", "events": "*"}, "lead.captured", False),
            ({"enabled": True, "events": "*"}, "tenant.settings_changed", False),
            (None, "lead.captured", False),
            ({}, "lead.captured", False),
        ]
        for config, event_type, expected in cases:
            with self.subTest(config=config, event_type=event_type):
                self.assertIs(adapter.wants(event_type, config), expected)


class HandleSkipTests(SimpleTestCase):
    def test_no_tenant(self):
        self.assertEqual(adapter.handle(ENVELOPE, None).status, "skipped")

    def test_turned_off(self):
        http = FakeHttp()
        self.assertEqual(adapter.handle(ENVELOPE, ctx(config={"enabled": False}, http=http)).status, "skipped")
        self.assertEqual(http.calls, [])

    def test_filtered_out(self):
        http = FakeHttp()
        result = adapter.handle(ENVELOPE, ctx(config={"events": "claim.*"}, http=http))
        self.assertEqual((result.status, http.calls), ("skipped", []))

    def test_loop_guard(self):
        http = FakeHttp()
        result = adapter.handle({**ENVELOPE, "origin": "webhook"}, ctx(http=http))
        self.assertEqual((result.status, http.calls), ("skipped", []))

    def test_missing_secrets_is_not_configured_not_a_failure(self):
        for secrets in ({}, {"url": URL}, {"signing_secret": KEY}):
            with self.subTest(secrets=sorted(secrets)):
                self.assertEqual(adapter.handle(ENVELOPE, ctx(secrets=secrets)).status, "skipped")


class RequestTests(SimpleTestCase):
    def send(self, **kw):
        http = FakeHttp()
        result = adapter.handle(ENVELOPE, ctx(http=http, **kw))
        return result, http.calls[0]

    def test_a_signed_json_post_to_the_secret_url(self):
        result, call = self.send()
        self.assertEqual(result.status, "ok")
        self.assertEqual((call["method"], call["url"]), ("POST", URL))
        self.assertEqual(json.loads(call["body"]), ENVELOPE)

    def test_the_headers(self):
        _, call = self.send()
        h = call["headers"]
        self.assertEqual(h["Content-Type"], "application/json")
        self.assertEqual(h["X-OSDS-Event-Id"], "01TESTEVENT")
        self.assertEqual(h["X-OSDS-Event-Type"], "lead.captured")
        self.assertEqual(h["X-OSDS-Timestamp"], str(int(NOW.timestamp())))
        self.assertTrue(h["User-Agent"].startswith("OSDS-Webhook/"))

    def test_the_signature_is_over_the_exact_bytes_sent_and_verifies(self):
        _, call = self.send()
        self.assertTrue(
            signing.verify(KEY, call["headers"]["X-OSDS-Signature"], call["body"], now=int(NOW.timestamp()))
        )

    def test_a_different_secret_does_not_verify(self):
        _, call = self.send()
        self.assertFalse(
            signing.verify(KEY + "x", call["headers"]["X-OSDS-Signature"], call["body"], now=int(NOW.timestamp()))
        )

    def test_the_body_is_deterministic_compact_utf8(self):
        env = {**ENVELOPE, "data": {"name": "Zoë", "b": 1, "a": 2}}
        http = FakeHttp()
        adapter.handle(env, ctx(http=http))
        body = http.calls[0]["body"]
        self.assertEqual(body, json.dumps(env, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        self.assertIn("Zoë".encode("utf-8"), body)
        self.assertNotIn(b": ", body)

    def test_the_timeout_is_inside_the_drains_claim_window(self):
        _, call = self.send()
        self.assertLessEqual(call["timeout"], 30)

    def test_an_oversized_event_is_dead_lettered_not_sent(self):
        http = FakeHttp()
        env = {**ENVELOPE, "data": {"blob": "x" * (MAX_BODY_BYTES + 1)}}
        result = adapter.handle(env, ctx(http=http))
        self.assertEqual((result.status, result.permanent, http.calls), ("failed", True, []))


class AnswerTests(SimpleTestCase):
    def answer(self, status, headers=None):
        return adapter.handle(ENVELOPE, ctx(http=FakeHttp(HttpResponse(status, b"body-we-never-keep", headers or {}))))

    def test_success(self):
        for status in (200, 201, 202, 204, 299):
            self.assertEqual(self.answer(status).status, "ok", status)

    def test_409_is_a_duplicate_which_is_success(self):
        result = self.answer(409)
        self.assertEqual((result.status, result.reason), ("ok", "http 409 duplicate"))

    def test_429_retries_and_carries_the_hint(self):
        result = self.answer(429, {"retry-after": "120"})
        self.assertEqual((result.status, result.retry_after_ms, result.reason), ("retry", 120_000, "http 429"))

    def test_429_without_a_usable_hint_asks_for_none(self):
        for headers in ({}, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, {"retry-after": "-5"}, {"retry-after": ""}):
            with self.subTest(headers=headers):
                result = self.answer(429, headers)
                self.assertEqual((result.status, result.retry_after_ms), ("retry", 0))

    def test_410_never_comes_back(self):
        result = self.answer(410)
        self.assertEqual((result.status, result.permanent, result.reason), ("failed", True, "http 410"))

    def test_other_client_and_server_errors_retry_with_the_status_as_the_reason(self):
        for status in (400, 401, 403, 404, 408, 500, 502, 503, 504):
            with self.subTest(status=status):
                result = self.answer(status)
                self.assertEqual((result.status, result.permanent, result.reason), ("failed", False, f"http {status}"))

    def test_redirects_are_not_followed_and_say_so(self):
        for status in (301, 302, 307, 308):
            result = self.answer(status, {"location": "https://elsewhere.example.test/"})
            self.assertEqual((result.status, result.permanent), ("failed", False))
            self.assertIn("redirects are not followed", result.reason)

    def test_the_response_body_is_never_kept(self):
        result = self.answer(500)
        self.assertNotIn("body-we-never-keep", result.reason)

    def test_an_egress_refusal_is_permanent(self):
        result = adapter.handle(ENVELOPE, ctx(http=FakeHttp(error=EgressDenied("resolves to a private address"))))
        self.assertEqual((result.status, result.permanent, result.reason), ("failed", True, "EgressDenied"))

    def test_network_errors_retry_and_record_only_the_class(self):
        for error in (ConnectionResetError("reset by peer https://x"), TimeoutError("slow"), OSError("boom")):
            with self.subTest(error=type(error).__name__):
                result = adapter.handle(ENVELOPE, ctx(http=FakeHttp(error=error)))
                self.assertEqual((result.status, result.permanent, result.reason), ("failed", False, type(error).__name__))


class LoggingTests(SimpleTestCase):
    def lines(self, http):
        with self.assertLogs("osds.adapters.webhook", "DEBUG") as logs:
            adapter.handle(ENVELOPE, ctx(http=http))
        return "\n".join(logs.output)

    def test_a_success_logs_the_event_and_the_status_only(self):
        out = self.lines(FakeHttp())
        self.assertIn("01TESTEVENT", out)
        self.assertIn("http 200", out)

    def test_nothing_secret_or_personal_is_ever_logged(self):
        for http in (
            FakeHttp(), FakeHttp(HttpResponse(503, b"x")), FakeHttp(error=ConnectionError(URL)),
            FakeHttp(error=EgressDenied(URL)),
        ):
            out = self.lines(http)
            for forbidden in (URL, "T0K3N", KEY, "hooks.example.test/in"):
                self.assertNotIn(forbidden, out)


class SettingsHookTests(SimpleTestCase):
    def test_fields(self):
        fields = {f.key: f for f in adapter.settings_fields()}
        self.assertEqual(set(fields), {"enabled", "url", "events"})
        self.assertTrue(fields["url"].secret)
        self.assertEqual(fields["enabled"].kind, "bool")

    def test_it_asks_for_both_scopes_and_nothing_else(self):
        self.assertEqual(adapter.scopes, frozenset({"pii:contact", "pii:message"}))

    def test_validate_accepts_a_good_submission(self):
        self.assertEqual(adapter.validate_settings({"enabled": True, "events": "claim.*, lead.captured", "url": URL}, stored=set()), [])

    def test_validate_refuses_bad_patterns_urls_and_enabling_with_no_url(self):
        def errors(values, stored=frozenset()):
            return adapter.validate_settings(values, stored=set(stored))

        self.assertTrue(errors({"events": "tenant.*"}))
        self.assertTrue(errors({"events": "Claim!"}))
        self.assertTrue(errors({"url": "ftp://x.test/"}))
        self.assertTrue(errors({"url": "https:///nohost"}))
        self.assertTrue(errors({"url": "https://user:pw@hooks.example.test/"}))
        self.assertTrue(errors({"enabled": True}))
        self.assertEqual(errors({"enabled": True}, stored={"url"}), [])
        self.assertEqual(errors({"enabled": False}), [])

    def test_derive_the_host(self):
        self.assertEqual(adapter.derive_config({"url": "https://Hooks.Example.test:8443/a/b?c=d"}), {"host": "hooks.example.test"})
        self.assertEqual(adapter.derive_config({"url": ""}), {"host": ""})  # the URL was removed
        self.assertEqual(adapter.derive_config({"enabled": True}), {})      # the stored URL is unchanged

    def test_the_egress_allowlist_is_the_configured_host_only(self):
        self.assertEqual(adapter.egress_allowlist_for({"host": "Hooks.Example.test"}), ("hooks.example.test",))
        self.assertEqual(adapter.egress_allowlist_for({}), ())

    def test_generated_secrets_are_long_random_and_prefixed(self):
        a, b = adapter.generate_secret("signing_secret"), adapter.generate_secret("signing_secret")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("whsec_"))
        self.assertGreaterEqual(len(a), 70)
        self.assertEqual(adapter.rotatable_secrets, ("signing_secret",))


class SendTestTests(SimpleTestCase):
    def test_a_test_event_is_signed_and_labelled(self):
        http = FakeHttp()
        ok, detail = adapter.send_test(ctx(http=http))
        self.assertEqual((ok, detail), (True, "http 200"))
        call = http.calls[0]
        body = json.loads(call["body"])
        self.assertEqual(body["type"], "webhook.test")
        self.assertEqual(body["redacted"], [])
        self.assertEqual(body["tenant"]["slug"], "acme")
        self.assertTrue(signing.verify(KEY, call["headers"]["X-OSDS-Signature"], call["body"], now=int(NOW.timestamp())))

    def test_it_does_not_need_the_endpoint_to_be_turned_on(self):
        http = FakeHttp()
        ok, _ = adapter.send_test(ctx(config={"enabled": False}, http=http))
        self.assertTrue(ok)

    def test_failure_says_why_in_words_without_the_body(self):
        ok, detail = adapter.send_test(ctx(http=FakeHttp(HttpResponse(503, b"secret-echo"))))
        self.assertEqual((ok, detail), (False, "http 503"))

    def test_nothing_configured(self):
        ok, detail = adapter.send_test(ctx(secrets={}))
        self.assertFalse(ok)
        self.assertIn("endpoint URL", detail)

    def test_an_egress_refusal(self):
        ok, detail = adapter.send_test(ctx(http=FakeHttp(error=EgressDenied("x"))))
        self.assertEqual((ok, detail), (False, "EgressDenied"))


class RetryAfterTests(SimpleTestCase):
    def test_parsing(self):
        self.assertEqual(_retry_after_ms({"retry-after": "7"}), 7000)
        self.assertEqual(_retry_after_ms({"retry-after": "0"}), 0)
        self.assertEqual(_retry_after_ms({}), 0)
        self.assertEqual(_retry_after_ms(None), 0)
