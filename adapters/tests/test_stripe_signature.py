"""Stripe webhook signature verification (``adapters.stripe.signature``).

Stripe's documented scheme -- ``t=<ts>,v1=<hmac>`` over ``"{t}.{raw body}"`` with
the endpoint secret, every ``v1`` compared in constant time, other schemes
ignored, five-minute tolerance -- is exercised three ways: the vectors below are
built independently with ``hashlib`` straight from that description, two are
pinned as fixed strings so a change to the algorithm cannot pass by changing
both sides, and the rest are the failure modes.

Stripe's documentation shows an example header but no complete
(secret, payload, signature) triple, so the pinned values were computed here
from the documented algorithm rather than copied from Stripe.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest import mock

from django.test import SimpleTestCase

from adapters.stripe import signature
from adapters.stripe.signature import SignatureError, TOLERANCE_SECONDS
from adapters.tests.stripe_fakes import NOW, WEBHOOK_SECRET, sign

BODY = b'{"id":"evt_1","object":"event"}'
T = int(NOW.timestamp())

# Fixed vectors, computed once from the documented scheme.
PINNED = [
    (
        "whsec_test_secret_0123456789", 1492774577,
        b'{\n  "id": "evt_test",\n  "object": "event"\n}',
        "f510cda5dac86a82a716e6a18367066950d2eadae6475aa57c5f61f7f14f1182",
    ),
    (
        "whsec_other", 1700000000, b'{"id":"evt_1","type":"invoice.paid"}',
        "05074d0afb23c996d440f69b158e2062e80029a9afc33bb81c16b20d636a2063",
    ),
]


class VectorTests(SimpleTestCase):
    def test_the_pinned_vectors_verify(self):
        for secret, t, body, digest in PINNED:
            with self.subTest(t=t):
                header = f"t={t},v1={digest}"
                self.assertEqual(signature.verify(header, body, secret, now=t), t)
                self.assertEqual(signature.expected_signature(secret, t, body), digest)

    def test_the_documented_scheme_built_independently(self):
        digest = hmac.new(WEBHOOK_SECRET.encode(), f"{T}.".encode() + BODY, hashlib.sha256).hexdigest()
        self.assertEqual(signature.verify(f"t={T},v1={digest}", BODY, WEBHOOK_SECRET, now=T), T)
        self.assertEqual(signature.verify(sign(BODY), BODY, WEBHOOK_SECRET, now=T), T)

    def test_the_signed_payload_is_the_timestamp_a_dot_and_the_raw_bytes(self):
        digest = hmac.new(b"k", b"123.hello", hashlib.sha256).hexdigest()
        self.assertEqual(signature.expected_signature("k", 123, b"hello"), digest)

    def test_the_header_may_carry_v0_and_extra_whitespace_in_any_order(self):
        good = sign(BODY).split("v1=")[1]
        for header in (
            f"v0=00,t={T},v1={good}",
            f"t={T}, v1={good}, v0=00",
            f"  t={T} ,  v1={good}  ",
            f"v1={good},t={T}",
        ):
            with self.subTest(header=header):
                self.assertEqual(signature.verify(header, BODY, WEBHOOK_SECRET, now=T), T)


class RolledSecretTests(SimpleTestCase):
    def test_any_one_of_several_v1_signatures_may_match(self):
        old = sign(BODY, "whsec_old").split("v1=")[1]
        new = sign(BODY, WEBHOOK_SECRET).split("v1=")[1]
        for order in ((old, new), (new, old)):
            header = f"t={T}," + ",".join(f"v1={s}" for s in order)
            self.assertEqual(signature.verify(header, BODY, WEBHOOK_SECRET, now=T), T)

    def test_every_v1_is_compared_with_the_constant_time_function(self):
        header = f"t={T},v1={'0' * 64},v1={'1' * 64},v1={'2' * 64}"
        with mock.patch("adapters.stripe.signature.hmac.compare_digest", return_value=False) as spy:
            with self.assertRaises(SignatureError):
                signature.verify(header, BODY, WEBHOOK_SECRET, now=T)
        self.assertEqual(spy.call_count, 3)  # no early exit


class FailureTests(SimpleTestCase):
    def reason(self, header, body=BODY, secret=WEBHOOK_SECRET, now=T):
        with self.assertRaises(SignatureError) as cm:
            signature.verify(header, body, secret, now=now)
        return cm.exception.reason

    def test_the_wrong_secret(self):
        self.assertEqual(self.reason(sign(BODY, "whsec_wrong")), "no_match")

    def test_a_body_changed_after_signing(self):
        self.assertEqual(self.reason(sign(BODY), body=BODY + b" "), "no_match")
        self.assertEqual(self.reason(sign(BODY), body=BODY.replace(b"evt_1", b"evt_2")), "no_match")

    def test_a_reserialised_body_does_not_verify(self):
        import json

        pretty = json.dumps(json.loads(BODY), indent=2).encode()
        self.assertEqual(self.reason(sign(BODY), body=pretty), "no_match")

    def test_a_timestamp_changed_after_signing(self):
        digest = sign(BODY).split("v1=")[1]
        self.assertEqual(self.reason(f"t={T + 1},v1={digest}", now=T + 1), "no_match")

    def test_only_the_v0_scheme_is_ignored_not_honoured(self):
        digest = sign(BODY).split("v1=")[1]
        self.assertEqual(self.reason(f"t={T},v0={digest}"), "no_v1_signature")

    def test_the_tolerance_is_five_minutes_either_way(self):
        header = sign(BODY)
        self.assertEqual(TOLERANCE_SECONDS, 300)
        signature.verify(header, BODY, WEBHOOK_SECRET, now=T + 300)
        signature.verify(header, BODY, WEBHOOK_SECRET, now=T - 300)
        self.assertEqual(self.reason(header, now=T + 301), "stale_timestamp")
        self.assertEqual(self.reason(header, now=T - 301), "stale_timestamp")
        self.assertEqual(self.reason(header, now=T + 86400), "stale_timestamp")

    def test_a_replay_of_a_captured_request_is_refused_once_it_is_stale(self):
        captured = sign(BODY)
        signature.verify(captured, BODY, WEBHOOK_SECRET, now=T + 10)
        self.assertEqual(self.reason(captured, now=T + 3600), "stale_timestamp")

    def test_a_stale_request_with_a_wrong_signature_says_no_match_first(self):
        self.assertEqual(self.reason(sign(BODY, "whsec_wrong"), now=T + 9999), "no_match")

    def test_malformed_headers(self):
        cases = {
            "": "no_timestamp",
            "garbage": "no_timestamp",
            "v1=abc": "no_timestamp",
            "t=abc,v1=ab": "bad_timestamp",
            "t=,v1=ab": "bad_timestamp",
            f"t={T}": "no_v1_signature",
            f"t={T},v1=": "no_v1_signature",
            f"t={T},v1": "no_v1_signature",
            ",,,": "no_timestamp",
        }
        for header, reason in cases.items():
            with self.subTest(header=header):
                self.assertEqual(self.reason(header), reason)

    def test_a_non_ascii_signature_is_a_mismatch_not_a_crash(self):
        self.assertEqual(self.reason(f"t={T},v1=é€"), "no_match")

    def test_an_empty_body_is_just_a_body(self):
        self.assertEqual(signature.verify(sign(b""), b"", WEBHOOK_SECRET, now=T), T)

    def test_the_tolerance_is_not_configuration(self):
        import inspect

        self.assertNotIn("tolerance", inspect.signature(signature.verify).parameters)
