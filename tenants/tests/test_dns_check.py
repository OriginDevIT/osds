"""tenants.dns_check.check_domain_http -- HTTP challenge verification."""

from __future__ import annotations

import urllib.error
from contextlib import contextmanager
from unittest import mock

from django.test import SimpleTestCase

from tenants.dns_check import check_domain_http


@contextmanager
def _urlopen_returning(body: bytes):
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    with mock.patch(
        "tenants.dns_check.urllib.request.urlopen", return_value=resp
    ) as m:
        yield m


class CheckDomainHttpTests(SimpleTestCase):
    def test_match_verifies(self):
        with _urlopen_returning(b"challenge-token-abc\n"):
            ok, detail = check_domain_http("acme.example", "challenge-token-abc")
        self.assertTrue(ok)
        self.assertIn("HTTP", detail)

    def test_body_mismatch_fails(self):
        with _urlopen_returning(b"something-else"):
            ok, detail = check_domain_http("acme.example", "challenge-token-abc")
        self.assertFalse(ok)
        self.assertIn("mismatch", detail)

    def test_unreachable_fails_without_raising(self):
        with mock.patch(
            "tenants.dns_check.urllib.request.urlopen",
            side_effect=urllib.error.URLError("name resolution failed"),
        ):
            ok, detail = check_domain_http("nope.example", "tok")
        self.assertFalse(ok)
        self.assertIn("could not reach", detail)

    def test_requests_the_wellknown_path_over_http(self):
        with _urlopen_returning(b"tok") as m:
            check_domain_http("acme.example", "tok")
        (called_request,), kwargs = m.call_args
        self.assertEqual(
            called_request.full_url,
            "http://acme.example/.well-known/osds-challenge",
        )
        self.assertLessEqual(kwargs.get("timeout", 999), 5)

    def test_empty_inputs_fail_fast(self):
        self.assertFalse(check_domain_http("", "tok")[0])
        self.assertFalse(check_domain_http("acme.example", "")[0])
