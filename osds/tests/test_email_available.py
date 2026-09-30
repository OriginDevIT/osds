"""``osds.adapters.email_available`` -- core's one question about mail
(decisions.md §4.5)."""

from __future__ import annotations

from django.test import SimpleTestCase

from osds.adapters import email_available, override_capability
from osds.tests.mail_stub import email_send_stub


class _Tenant:
    settings: dict = {}


class EmailAvailableTests(SimpleTestCase):
    def test_false_with_no_provider(self):
        with override_capability({}):
            self.assertFalse(email_available(_Tenant()))

    def test_false_for_a_provider_without_available(self):
        class Bare:
            def send(self, message): ...

        with override_capability({"email.send": Bare()}):
            self.assertFalse(email_available(_Tenant()))

    def test_delegates_to_the_provider(self):
        with email_send_stub(available=True):
            self.assertTrue(email_available(_Tenant()))
        with email_send_stub(available=False):
            self.assertFalse(email_available(_Tenant()))
