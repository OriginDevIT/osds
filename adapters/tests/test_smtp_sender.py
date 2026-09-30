"""The bundled ``smtp`` sender (spec §8.6, decisions.md §4.3).

Unit-tested against a fake ``smtplib.SMTP`` -- no Mailpit, no network, no
extra container for the suite to depend on.
"""

from __future__ import annotations

import smtplib
import ssl
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from adapters.smtp.sender import SmtpSender
from osds.adapters import CapabilityUnconfigured
from tenants.models import Tenant
from tenants.secrets import set_secret


def _message(tenant, *, to_address="claimant@example.test", body_text="123456"):
    return SimpleNamespace(
        message_id="msg_01JTEST0000000000000000",
        tenant=tenant,
        subject="Your verification code",
        to_address=to_address,
        body_text=body_text,
    )


def _fake_client(*, starttls_exc=None, login_exc=None, send_exc=None):
    client = mock.MagicMock(name="smtplib.SMTP instance")
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    if starttls_exc is not None:
        client.starttls.side_effect = starttls_exc
    if login_exc is not None:
        client.login.side_effect = login_exc
    if send_exc is not None:
        client.send_message.side_effect = send_exc
    return client


class SmtpSenderTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")
        self.sender = SmtpSender()

    def _configure(self, **overrides):
        cfg = {
            "host": "smtp.example.test",
            "port": 587,
            "from_email": "noreply@example.test",
            "username": "",
            "security": "starttls",
        }
        cfg.update(overrides)
        self.tenant.settings = {"smtp": cfg}

    # -- configuration --------------------------------------------------
    def test_missing_host_raises_capability_unconfigured(self):
        self._configure(host="")
        with self.assertRaises(CapabilityUnconfigured):
            self.sender.send(_message(self.tenant))

    def test_missing_from_email_raises_capability_unconfigured(self):
        self._configure(from_email="")
        with self.assertRaises(CapabilityUnconfigured):
            self.sender.send(_message(self.tenant))

    def test_missing_smtp_settings_entirely_raises_capability_unconfigured(self):
        self.tenant.settings = {}
        with self.assertRaises(CapabilityUnconfigured):
            self.sender.send(_message(self.tenant))

    def test_no_smtp_call_is_made_when_unconfigured(self):
        self._configure(host="")
        with mock.patch("adapters.smtp.sender.smtplib.SMTP") as smtp_cls:
            with self.assertRaises(CapabilityUnconfigured):
                self.sender.send(_message(self.tenant))
        smtp_cls.assert_not_called()

    # -- success ----------------------------------------------------------
    def test_ok_send_returns_ok_and_uses_configured_host_port_and_timeout(self):
        self._configure(host="smtp.example.test", port=2525)
        client = _fake_client()
        with mock.patch(
            "adapters.smtp.sender.smtplib.SMTP", return_value=client
        ) as smtp_cls:
            result = self.sender.send(_message(self.tenant))
        smtp_cls.assert_called_once_with("smtp.example.test", 2525, timeout=30)
        self.assertEqual(result.status, "ok")
        client.send_message.assert_called_once()

    def test_starttls_passes_a_verifying_context(self):
        self._configure(security="starttls")
        client = _fake_client()
        with mock.patch("adapters.smtp.sender.smtplib.SMTP", return_value=client):
            self.sender.send(_message(self.tenant))
        client.starttls.assert_called_once()
        ctx = client.starttls.call_args.kwargs["context"]
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_tls_uses_smtp_ssl_with_a_verifying_context(self):
        self._configure(security="tls", port=465)
        client = _fake_client()
        with mock.patch(
            "adapters.smtp.sender.smtplib.SMTP_SSL", return_value=client
        ) as ssl_cls, mock.patch("adapters.smtp.sender.smtplib.SMTP") as plain_cls:
            result = self.sender.send(_message(self.tenant))
        self.assertEqual(result.status, "ok")
        plain_cls.assert_not_called()
        client.starttls.assert_not_called()
        args, kwargs = ssl_cls.call_args
        self.assertEqual(args, ("smtp.example.test", 465))
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kwargs["context"].check_hostname)

    def test_none_skips_starttls(self):
        self._configure(security="none")
        client = _fake_client()
        with mock.patch("adapters.smtp.sender.smtplib.SMTP", return_value=client):
            self.sender.send(_message(self.tenant))
        client.starttls.assert_not_called()

    def test_starttls_not_offered_fails_the_send_and_never_sends_in_clear(self):
        self._configure(security="starttls")
        client = _fake_client(
            starttls_exc=smtplib.SMTPNotSupportedError("STARTTLS not supported")
        )
        with mock.patch("adapters.smtp.sender.smtplib.SMTP", return_value=client):
            result = self.sender.send(_message(self.tenant))
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.permanent)
        client.send_message.assert_not_called()

    def test_certificate_failure_retries(self):
        self._configure(security="tls")
        with mock.patch(
            "adapters.smtp.sender.smtplib.SMTP_SSL",
            side_effect=ssl.SSLCertVerificationError("bad cert"),
        ):
            result = self.sender.send(_message(self.tenant))
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.permanent)

    def test_username_with_security_none_is_unconfigured_and_never_connects(self):
        self._configure(security="none", username="bot")
        with mock.patch("adapters.smtp.sender.smtplib.SMTP") as smtp_cls:
            with self.assertRaises(CapabilityUnconfigured):
                self.sender.send(_message(self.tenant))
        smtp_cls.assert_not_called()

    def test_missing_or_unknown_security_is_unconfigured(self):
        for security in (None, "", "ssl", "STARTTLS"):
            with self.subTest(security=security):
                self._configure(security=security)
                with self.assertRaises(CapabilityUnconfigured):
                    self.sender.send(_message(self.tenant))

    def test_the_legacy_use_tls_key_is_not_read(self):
        self._configure(use_tls=True)
        del self.tenant.settings["smtp"]["security"]
        with self.assertRaises(CapabilityUnconfigured):
            self.sender.send(_message(self.tenant))

    # -- available() ------------------------------------------------------
    def test_available_truth_table(self):
        cases = [
            ({}, True),
            ({"security": "tls"}, True),
            ({"security": "none"}, True),
            ({"security": "none", "username": "bot"}, False),
            ({"security": "tls", "username": "bot"}, True),
            ({"host": ""}, False),
            ({"host": "  "}, False),
            ({"from_email": ""}, False),
            ({"security": "bogus"}, False),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self._configure(**overrides)
                self.assertIs(self.sender.available(self.tenant), expected)

    def test_available_false_with_no_settings_or_an_empty_skip_block(self):
        self.tenant.settings = {}
        self.assertFalse(self.sender.available(self.tenant))
        self.tenant.settings = {"smtp": {}}
        self.assertFalse(self.sender.available(self.tenant))

    def test_available_means_configured_not_reachable(self):
        self._configure()
        with mock.patch("adapters.smtp.sender.smtplib.SMTP") as smtp_cls:
            self.assertTrue(self.sender.available(self.tenant))
        smtp_cls.assert_not_called()

    def test_username_resolves_the_secret_password_and_logs_in(self):
        self._configure(username="bot")
        set_secret("smtp_password", "s3cret", tenant=self.tenant)
        client = _fake_client()
        with mock.patch("adapters.smtp.sender.smtplib.SMTP", return_value=client):
            self.sender.send(_message(self.tenant))
        client.login.assert_called_once_with("bot", "s3cret")

    def test_no_username_skips_login(self):
        self._configure(username="")
        client = _fake_client()
        with mock.patch("adapters.smtp.sender.smtplib.SMTP", return_value=client):
            self.sender.send(_message(self.tenant))
        client.login.assert_not_called()

    def test_from_and_to_and_subject_are_set_on_the_message(self):
        self._configure(from_email="noreply@example.test")
        client = _fake_client()
        with mock.patch("adapters.smtp.sender.smtplib.SMTP", return_value=client):
            self.sender.send(_message(self.tenant, to_address="claimant@example.test"))
        sent = client.send_message.call_args[0][0]
        self.assertEqual(sent["From"], "noreply@example.test")
        self.assertEqual(sent["To"], "claimant@example.test")
        self.assertEqual(sent["Subject"], "Your verification code")

    # -- permanent failure: recipient refusal -----------------------------
    def test_recipients_refused_is_permanent(self):
        self._configure()
        exc = smtplib.SMTPRecipientsRefused({"claimant@example.test": (550, b"no such user")})
        client = _fake_client(send_exc=exc)
        with mock.patch("adapters.smtp.sender.smtplib.SMTP", return_value=client):
            result = self.sender.send(_message(self.tenant))
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.permanent)
        self.assertEqual(result.reason, "SMTPRecipientsRefused")

    def test_5xx_on_data_is_permanent(self):
        self._configure()
        exc = smtplib.SMTPDataError(550, b"message refused")
        client = _fake_client(send_exc=exc)
        with mock.patch("adapters.smtp.sender.smtplib.SMTP", return_value=client):
            result = self.sender.send(_message(self.tenant))
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.permanent)

    # -- retryable failures ------------------------------------------------
    def test_4xx_on_data_retries(self):
        self._configure()
        exc = smtplib.SMTPDataError(450, b"try again later")
        client = _fake_client(send_exc=exc)
        with mock.patch("adapters.smtp.sender.smtplib.SMTP", return_value=client):
            result = self.sender.send(_message(self.tenant))
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.permanent)

    def test_auth_failure_retries(self):
        self._configure(username="bot")
        set_secret("smtp_password", "wrong", tenant=self.tenant)
        exc = smtplib.SMTPAuthenticationError(535, b"authentication failed")
        client = _fake_client(login_exc=exc)
        with mock.patch("adapters.smtp.sender.smtplib.SMTP", return_value=client):
            result = self.sender.send(_message(self.tenant))
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.permanent)

    def test_sender_refused_retries(self):
        self._configure()
        exc = smtplib.SMTPSenderRefused(550, b"sender refused", "noreply@example.test")
        client = _fake_client(send_exc=exc)
        with mock.patch("adapters.smtp.sender.smtplib.SMTP", return_value=client):
            result = self.sender.send(_message(self.tenant))
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.permanent)

    def test_connection_error_retries(self):
        self._configure()
        with mock.patch(
            "adapters.smtp.sender.smtplib.SMTP",
            side_effect=ConnectionRefusedError("connection refused"),
        ):
            result = self.sender.send(_message(self.tenant))
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.permanent)

    # -- §8.3: no contact value ever logged --------------------------------
    def test_failure_reason_never_contains_the_recipient_address(self):
        self._configure()
        exc = smtplib.SMTPRecipientsRefused(
            {"claimant@example.test": (550, b"claimant@example.test unknown")}
        )
        client = _fake_client(send_exc=exc)
        with mock.patch("adapters.smtp.sender.smtplib.SMTP", return_value=client):
            result = self.sender.send(_message(self.tenant))
        self.assertNotIn("claimant@example.test", result.reason)
        self.assertEqual(result.reason, "SMTPRecipientsRefused")

    def test_log_line_on_failure_contains_no_contact_value(self):
        self._configure()
        exc = smtplib.SMTPRecipientsRefused(
            {"claimant@example.test": (550, b"claimant@example.test unknown")}
        )
        client = _fake_client(send_exc=exc)
        message = _message(self.tenant, body_text="the-secret-code-987654")
        with mock.patch("adapters.smtp.sender.smtplib.SMTP", return_value=client):
            with self.assertLogs("osds.mail.smtp", level="WARNING") as captured:
                self.sender.send(message)
        joined = "\n".join(captured.output)
        self.assertNotIn("claimant@example.test", joined)
        self.assertNotIn("the-secret-code-987654", joined)
        self.assertIn(message.message_id, joined)
        self.assertIn("SMTPRecipientsRefused", joined)
