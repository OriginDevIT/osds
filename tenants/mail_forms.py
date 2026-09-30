"""The mail settings form, shared by the first-run wizard and the tenant-admin
page at ``/admin/settings/mail/`` (decisions.md §4.5)."""

from __future__ import annotations

from django import forms

from tenants.mail_settings import (
    DEFAULT_SMTP_PORT,
    HOST_CHANGE_MESSAGE,
    SMTP_SECURITY_CHOICES,
    default_from_email,
    host_change_needs_password,
)
from tenants.secrets import has_secret


class MailSettingsForm(forms.Form):
    host = forms.CharField(label="SMTP host")
    security = forms.ChoiceField(
        label="Connection security",
        choices=SMTP_SECURITY_CHOICES,
        initial="starttls",
        help_text=(
            "STARTTLS usually runs on port 587, implicit TLS on 465 and "
            "none on 25. Certificates are always verified. A username "
            "requires STARTTLS or TLS."
        ),
    )
    port = forms.IntegerField(
        label="Port", min_value=1, max_value=65535, initial=DEFAULT_SMTP_PORT
    )
    from_email = forms.EmailField(label="From address")
    username = forms.CharField(label="Username", required=False)
    password = forms.CharField(
        label="Password",
        required=False,
        widget=forms.PasswordInput(render_value=False),
    )
    clear_password = forms.BooleanField(
        label="Remove the stored password", required=False
    )

    def __init__(self, *args, tenant, **kwargs):
        self.tenant = tenant
        stored = (tenant.settings or {}).get("smtp") or {}
        initial = {
            k: stored[k]
            for k in ("host", "security", "port", "from_email", "username")
            if stored.get(k) not in (None, "")
        }
        # A pre-fill, not a send-time default: an empty stored from_email
        # stays unconfigured until the admin saves this form.
        initial.setdefault("from_email", default_from_email(tenant))
        kwargs["initial"] = {**initial, **kwargs.get("initial", {})}
        super().__init__(*args, **kwargs)
        self.has_stored_password = has_secret("smtp_password", tenant=tenant)
        if self.has_stored_password:
            self.fields["password"].help_text = (
                "A password is stored. Leave blank to keep it."
            )

    def clean_host(self):
        return self.cleaned_data["host"].strip()

    def clean_username(self):
        return self.cleaned_data["username"].strip()

    def clean(self):
        cleaned = super().clean()
        username = cleaned.get("username") or ""
        password = cleaned.get("password") or ""
        if password and cleaned.get("clear_password"):
            self.add_error(
                "clear_password", "Enter a new password or remove it, not both."
            )
        if username and cleaned.get("security") == "none":
            self.add_error(
                "security",
                "A username requires STARTTLS or TLS: credentials are never "
                "sent in the clear.",
            )
        if (
            "host" in cleaned
            and not password
            and not cleaned.get("clear_password")
            and host_change_needs_password(
                self.tenant, host=cleaned["host"], username=username
            )
        ):
            self.add_error("password", HOST_CHANGE_MESSAGE)
        return cleaned

    def config(self) -> dict:
        data = self.cleaned_data
        return {
            "host": data["host"],
            "port": data["port"],
            "security": data["security"],
            "from_email": data["from_email"],
            "username": data["username"],
        }
