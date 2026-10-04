"""The bundled ``smtp`` sender (spec §8.6, decisions.md §4.3, §4.5, §4.12).

stdlib only -- ``smtplib``, ``ssl`` and ``email.message.EmailMessage``, plain
text. Rejected: ``django.core.mail``, which contradicts the settled stdlib
wording.

An adapter imports ``osds.adapter_api`` and ``osds.adapters`` and nothing else
from the project (#215, spec §8.3): core hands it an ``AdapterContext`` and a
plain ``MailMessage``, and a boundary test holds everything under ``adapters/``
to that. Settings (``host``, ``port``, ``security``, ``username``,
``from_email``) arrive as ``ctx.config`` -- they live at ``tenant.settings["smtp"]``,
where the first-run wizard and the mail settings page already write them, so the
provider names ``config_key = "smtp"`` -- and are read on every send, never
cached. The password is the secret ``smtp_password``: ``ctx.secret("password")``.

``security`` is ``none``, ``starttls`` or ``tls`` (implicit TLS, ``SMTP_SSL``).
Both TLS modes verify the certificate and hostname through
``ssl.create_default_context()``; a bare ``starttls()`` would use the
stdlib's unverified context. A server that does not offer STARTTLS fails the
send -- it never downgrades. Credentials never travel in the clear: a
username with ``security`` ``none`` is "not available".

``available(ctx)`` is the one configured-or-not predicate (§4.5). It means
configured, not reachable. ``send`` raises ``CapabilityUnconfigured`` when it
is false, so the mail drain, the admin banner and ``claim.submit`` agree and
the row stays pending with no attempt consumed (§4.3).

Failure classification (§4.3, correcting the outbox drain's blanket "any
exception retries, permanent dead-letters" split): only a recipient refusal
-- ``SMTPRecipientsRefused``, or a 5xx response to ``DATA`` -- is permanent.
Auth failure, sender refusal, a 4xx response, a certificate failure and
connection errors all retry with backoff; ``expires_at`` is what eventually
retires those, not an attempt ceiling.

Logs through ``ctx.logger`` (secrets scrubbed, #179) and records the error
class only (§8.3, §4.3): never ``str(exc)``, which for ``SMTPResponseException``
subclasses can echo the refused address back from the server's own response text.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from osds.adapter_api import SecretNotFound
from osds.adapters import CapabilityUnconfigured, Result

_TIMEOUT_SECONDS = 30  # spec §8.2 handler timeout

SECURITY_MODES = ("none", "starttls", "tls")


class SmtpSender:
    """Registered as the ``email.send`` capability provider."""

    adapter_id = "smtp"
    # Mail settings predate the adapter runtime and stay where they are.
    config_key = "smtp"

    def available(self, ctx) -> bool:
        """Configured, not reachable (decisions.md §4.5)."""
        cfg = ctx.config
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

    def _fail(self, ctx, message, exc, *, permanent: bool = False) -> Result:
        # The message id and the error class only (§8.3): never str(exc).
        ctx.logger.warning(
            "outbound message %s failed: %s", message.message_id, type(exc).__name__
        )
        return Result.failed(type(exc).__name__, permanent=permanent)

    def send(self, ctx, message) -> Result:
        if not self.available(ctx):
            raise CapabilityUnconfigured("smtp is not configured for this tenant")
        cfg = ctx.config
        host = cfg["host"].strip()
        from_email = cfg["from_email"].strip()
        port = cfg.get("port") or 587
        security = cfg["security"]
        username = (cfg.get("username") or "").strip()
        password = ""
        if username:
            try:
                password = ctx.secret("password")
            except SecretNotFound:
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
            return self._fail(ctx, message, exc, permanent=True)
        except smtplib.SMTPDataError as exc:
            return self._fail(ctx, message, exc, permanent=500 <= exc.smtp_code < 600)
        except smtplib.SMTPException as exc:
            # auth failure, sender refused, 4xx, disconnect, STARTTLS not
            # offered -- all retry
            return self._fail(ctx, message, exc)
        except OSError as exc:
            # connection errors -- refused, timed out, unreachable -- and
            # certificate verification failures (ssl.SSLError is an OSError)
            return self._fail(ctx, message, exc)

        return Result.ok()
