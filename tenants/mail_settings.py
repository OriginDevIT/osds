"""The shape of ``tenant.settings["smtp"]`` as core knows it (decisions.md §4.5).

Core validates and renders this configuration; it never reads it to decide
whether mail can be sent -- that question goes to the ``email.send``
provider's ``available(ctx)`` through ``osds.adapters.email_available``.
"""

from __future__ import annotations

SMTP_SECURITY_MODES = ("none", "starttls", "tls")
SMTP_SECURITY_CHOICES = [
    ("starttls", "STARTTLS"),
    ("tls", "TLS (implicit)"),
    ("none", "None"),
]
DEFAULT_SMTP_PORT = 587


HOST_CHANGE_MESSAGE = (
    "The SMTP host changed: re-enter the password. The stored password is "
    "never sent to a new host."
)


def host_change_needs_password(tenant, *, host: str, username: str) -> bool:
    """True when saving ``host`` with ``username`` would hand the stored
    password to a different host. The caller raises when this is true and no
    password was supplied. Shared by the form and ``update_mail_settings``."""
    from tenants.secrets import has_secret

    if not (username or "").strip():
        return False
    previous = (tenant.settings or {}).get("smtp") or {}
    old_host = (previous.get("host") or "").strip().lower()
    new_host = (host or "").strip().lower()
    if new_host == old_host:
        return False
    return bool(old_host) or has_secret("smtp_password", tenant=tenant)


def default_from_email(tenant) -> str:
    """The form's pre-fill: ``noreply@`` plus ``primary_domain`` verbatim,
    blank when the tenant has no domain. Never a send-time default."""
    domain = (tenant.primary_domain or "").strip()
    return f"noreply@{domain}" if domain else ""
