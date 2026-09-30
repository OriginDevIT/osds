"""The bundled ``smtp`` sender (spec §8.6, decisions.md §4.3, §4.5).

stdlib only -- ``smtplib``, ``ssl`` and ``email.message.EmailMessage``, plain
text. Rejected: ``django.core.mail``, which contradicts the settled stdlib
wording.

Settings (``host``, ``port``, ``security``, ``username``, ``from_email``) are
read from ``tenant.settings["smtp"]`` on every send, never cached -- §8.1
config is per-tenant and resolved at runtime. The password is a ``Secret``,
resolved through ``tenants.secrets.get_secret``.

``security`` is ``none``, ``starttls`` or ``tls`` (implicit TLS, ``SMTP_SSL``).
Both TLS modes verify the certificate and hostname through
``ssl.create_default_context()``; a bare ``starttls()`` would use the
stdlib's unverified context. A server that does not offer STARTTLS fails the
send -- it never downgrades. Credentials never travel in the clear: a
username with ``security`` ``none`` is "not available".

``available(tenant)`` is the one configured-or-not predicate (§4.5). It means
configured, not reachable. ``send`` raises ``CapabilityUnconfigured`` when it
is false, so the mail drain, the admin banner and ``claim.submit`` agree and
the row stays pending with no attempt consumed (§4.3).

Failure classification (§4.3, correcting the outbox drain's blanket "any
exception retries, permanent dead-letters" split): only a recipient refusal
-- ``SMTPRecipientsRefused``, or a 5xx response to ``DATA`` -- is permanent.
Auth failure, sender refusal, a 4xx response, a certificate failure and
connection errors all retry with backoff; ``expires_at`` is what eventually
retires those, not an attempt ceiling.

Logs the error class only (§8.3, §4.3): never ``str(exc)``, which for
``SMTPResponseException`` subclasses can echo the refused address back from
the server's own response text.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from osds.adapters import CapabilityUnconfigured, Result
from tenants.secrets import ConfigurationError, get_secret

_TIMEOUT_SECONDS = 30  # spec §8.2 handler timeout

SECURITY_MODES = ("none", "starttls", "tls")

logger = logging.getLogger("osds.mail.smtp")


def _smtp_settings(tenant) -> dict:
    return (tenant.settings or {}).get("smtp") or {}


class SmtpSender:
    """Registered as the ``email.send`` capability provider."""

    def available(self, tenant) -> bool:
        """Configured, not reachable (decisions.md §4.5)."""
        cfg = _smtp_settings(tenant)
        if not (cfg.get("host") or "").strip():
            return False
        if not (cfg.get("from_email") or "").strip():
            return False
        security = cfg.get("security")
        if security not in SECURITY_MODES:
            return False
        if (cfg.get("username") or "").strip() and security == "none":
            return False  # no AUTH without TLS
        return True

    def _fail(self, message, exc, *, permanent: bool = False) -> Result:
        # public_id and the error class only (§8.3): never str(exc), which
        # for an SMTPResponseException subclass can echo the refused address
        # back from the server's own response text.
        logger.warning(
            "outbound message %s failed: %s",
            message.message_id,
            type(exc).__name__,
        )
        return Result.failed(type(exc).__name__, permanent=permanent)

    def send(self, message) -> Result:
        if not self.available(message.tenant):
            raise CapabilityUnconfigured(
                "smtp is not configured for this tenant"
            )
        cfg = _smtp_settings(message.tenant)
        host = cfg["host"].strip()
        from_email = cfg["from_email"].strip()
        port = cfg.get("port") or 587
        security = cfg["security"]
        username = (cfg.get("username") or "").strip()
        password = ""
        if username:
            try:
                password = get_secret("smtp_password", tenant=message.tenant)
            except ConfigurationError:
                password = ""

        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = from_email
        email["To"] = message.to_address
        email.set_content(message.body_text or "")

        try:
            if security == "tls":
                client = smtplib.SMTP_SSL(
                    host,
                    port,
                    timeout=_TIMEOUT_SECONDS,
                    context=ssl.create_default_context(),
                )
            else:
                client = smtplib.SMTP(host, port, timeout=_TIMEOUT_SECONDS)
            with client:
                if security == "starttls":
                    client.starttls(context=ssl.create_default_context())
                if username:
                    client.login(username, password)
                client.send_message(email)
        except smtplib.SMTPRecipientsRefused as exc:
            return self._fail(message, exc, permanent=True)
        except smtplib.SMTPDataError as exc:
            return self._fail(message, exc, permanent=500 <= exc.smtp_code < 600)
        except smtplib.SMTPException as exc:
            # auth failure, sender refused, 4xx, disconnect, STARTTLS not
            # offered -- all retry
            return self._fail(message, exc)
        except OSError as exc:
            # connection errors -- refused, timed out, unreachable -- and
            # certificate verification failures (ssl.SSLError is an OSError)
            return self._fail(message, exc)

        return Result.ok()
