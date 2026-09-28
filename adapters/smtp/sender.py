"""The bundled ``smtp`` sender (spec §8.6, decisions.md §4.3).

stdlib only -- ``smtplib`` and ``email.message.EmailMessage``, plain text.
Rejected: ``django.core.mail``, which contradicts the settled stdlib wording.

Settings (``host``, ``port``, ``use_tls``, ``username``, ``from_email``) are
read from ``tenant.settings["smtp"]`` on every send, never cached -- §8.1
config is per-tenant and resolved at runtime. The password is a ``Secret``,
resolved through ``tenants.secrets.get_secret``.

An empty or missing ``host``/``from_email`` is "not configured", not a
delivery failure: it raises ``CapabilityUnconfigured`` so the mail drain
leaves the row pending with no attempt consumed (§4.3) instead of retrying
or dead-lettering a tenant that has simply not finished setup yet.

Failure classification (§4.3, correcting the outbox drain's blanket "any
exception retries, permanent dead-letters" split): only a recipient refusal
-- ``SMTPRecipientsRefused``, or a 5xx response to ``DATA`` -- is permanent.
Auth failure, sender refusal, a 4xx response and connection errors all
retry with backoff; ``expires_at`` is what eventually retires those, not an
attempt ceiling.

Logs the error class only (§8.3, §4.3): never ``str(exc)``, which for
``SMTPResponseException`` subclasses can echo the refused address back from
the server's own response text.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from osds.adapters import CapabilityUnconfigured, Result
from tenants.secrets import ConfigurationError, get_secret

_TIMEOUT_SECONDS = 30  # spec §8.2 handler timeout

logger = logging.getLogger("osds.mail.smtp")


class SmtpSender:
    """Registered as the ``email.send`` capability provider."""

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
        cfg = (message.tenant.settings or {}).get("smtp") or {}
        host = (cfg.get("host") or "").strip()
        from_email = (cfg.get("from_email") or "").strip()
        if not host or not from_email:
            raise CapabilityUnconfigured(
                "smtp is not configured for this tenant"
            )

        port = cfg.get("port") or 587
        use_tls = bool(cfg.get("use_tls"))
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
            with smtplib.SMTP(host, port, timeout=_TIMEOUT_SECONDS) as client:
                if use_tls:
                    client.starttls()
                if username:
                    client.login(username, password)
                client.send_message(email)
        except smtplib.SMTPRecipientsRefused as exc:
            return self._fail(message, exc, permanent=True)
        except smtplib.SMTPDataError as exc:
            return self._fail(message, exc, permanent=500 <= exc.smtp_code < 600)
        except smtplib.SMTPException as exc:
            # auth failure, sender refused, 4xx, disconnect -- all retry
            return self._fail(message, exc)
        except OSError as exc:
            # connection errors -- refused, timed out, unreachable
            return self._fail(message, exc)

        return Result.ok()
