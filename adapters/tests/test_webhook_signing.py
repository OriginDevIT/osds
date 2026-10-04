"""The webhook signature (decisions.md §4.12): ``t=<unix>,v1=<hmac-sha256>`` over
``"<t>.<raw body>"``. The vectors are computed here with the standard library,
independently of the module, so the format cannot drift silently.
"""

from __future__ import annotations

import hashlib
import hmac

from django.test import SimpleTestCase

from adapters.webhook import signing

SECRET = "whsec_" + "ab" * 32
BODY = b'{"id":"01ABC","type":"lead.captured"}'
NOW = 1_760_000_000


def reference(secret, timestamp, body):
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


class SignTests(SimpleTestCase):
    def test_the_signature_is_hmac_sha256_over_timestamp_dot_body(self):
        self.assertEqual(signing.signature(SECRET, NOW, BODY), reference(SECRET, NOW, BODY))

    def test_the_header_format(self):
        self.assertEqual(signing.header(SECRET, NOW, BODY), f"t={NOW},v1={reference(SECRET, NOW, BODY)}")

    def test_a_known_answer(self):
        # Fixed vector: a regression guard on the exact string being signed.
        self.assertEqual(
            signing.signature("key", 1, b"body"),
            hmac.new(b"key", b"1.body", hashlib.sha256).hexdigest(),
        )

    def test_the_timestamp_is_part_of_what_is_signed(self):
        self.assertNotEqual(signing.signature(SECRET, NOW, BODY), signing.signature(SECRET, NOW + 1, BODY))

    def test_the_key_matters(self):
        self.assertNotEqual(signing.signature(SECRET, NOW, BODY), signing.signature(SECRET + "x", NOW, BODY))

    def test_non_ascii_bodies(self):
        body = '{"name":"Zoë 日本"}'.encode("utf-8")
        self.assertEqual(signing.signature(SECRET, NOW, body), reference(SECRET, NOW, body))


class VerifyTests(SimpleTestCase):
    def good(self):
        return signing.header(SECRET, NOW, BODY)

    def test_a_good_signature_verifies(self):
        self.assertTrue(signing.verify(SECRET, self.good(), BODY, now=NOW))

    def test_a_tampered_body_does_not(self):
        self.assertFalse(signing.verify(SECRET, self.good(), BODY + b" ", now=NOW))

    def test_the_wrong_key_does_not(self):
        self.assertFalse(signing.verify(SECRET + "x", self.good(), BODY, now=NOW))

    def test_a_replay_outside_the_tolerance_does_not(self):
        self.assertTrue(signing.verify(SECRET, self.good(), BODY, now=NOW + signing.TOLERANCE_SECONDS))
        self.assertFalse(signing.verify(SECRET, self.good(), BODY, now=NOW + signing.TOLERANCE_SECONDS + 1))
        self.assertFalse(signing.verify(SECRET, self.good(), BODY, now=NOW - signing.TOLERANCE_SECONDS - 1))

    def test_a_forged_timestamp_breaks_the_signature(self):
        forged = f"t={NOW + 100},v1={reference(SECRET, NOW, BODY)}"
        self.assertFalse(signing.verify(SECRET, forged, BODY, now=NOW + 100))

    def test_malformed_headers_do_not_verify_and_do_not_raise(self):
        for value in ("", "garbage", "t=abc,v1=ff", f"v1={reference(SECRET, NOW, BODY)}", f"t={NOW}", "t=,v1="):
            with self.subTest(value=value):
                self.assertFalse(signing.verify(SECRET, value, BODY, now=NOW))
