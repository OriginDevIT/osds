from __future__ import annotations

import re

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from tenants.claim_verification import CLAIM_VERIFICATION_BOUNDS
from tenants.models import Tenant
from tenants.storage_forms import StorageForm  # noqa: F401  (shared with the admin Storage page)

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)


class SetupTokenForm(forms.Form):
    token = forms.CharField(label="Setup token", strip=True)


class AccountForm(forms.Form):
    name = forms.CharField(label="Your name", required=False)
    email = forms.EmailField(label="Email")
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The passwords do not match.")
        elif p1:
            try:
                validate_password(p1)
            except ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned


class DirectoryForm(forms.Form):
    name = forms.CharField(label="Directory name")
    slug = forms.SlugField(label="Slug", help_text="Lowercase, used internally.")
    mode = forms.ChoiceField(label="Mode", choices=Tenant.Mode.choices)

    def clean_slug(self):
        slug = self.cleaned_data["slug"].strip().lower()
        if Tenant.objects.filter(slug=slug).exists():
            raise ValidationError("That slug is already taken.")
        return slug


class DomainForm(forms.Form):
    domain = forms.CharField(
        label="Domain",
        help_text=(
            "The hostname the directory will be served on. Add :port if it is "
            "not served on the default port."
        ),
    )

    def clean_domain(self):
        raw = self.cleaned_data["domain"].strip().lower()
        host, sep, port_text = raw.partition(":")
        host = host.rstrip(".")
        if not _HOSTNAME_RE.match(host):
            raise ValidationError("Enter a valid domain, e.g. directory.example.com.")
        port = None
        if sep:
            if not (port_text.isascii() and port_text.isdigit()) or not (
                1 <= int(port_text) <= 65535
            ):
                raise ValidationError("Enter a valid port between 1 and 65535.")
            port = int(port_text)
        # Tenant routing keys on the bare host (the Host middleware drops the
        # port), so the port is carried separately.
        self.port = port
        return host


class ClaimsForm(forms.Form):
    METHODS = [
        ("manual", "Manual admin review (always on)"),
        ("domain_email", "Domain email"),
    ]
    methods = forms.MultipleChoiceField(
        label="Enabled verification methods",
        choices=METHODS,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    _domain_email_bounds = CLAIM_VERIFICATION_BOUNDS["domain_email"]
    domain_email_ttl_minutes = forms.IntegerField(
        label="Domain-email code lifetime (minutes)",
        min_value=_domain_email_bounds["min_minutes"],
        max_value=_domain_email_bounds["max_minutes"],
        initial=_domain_email_bounds["default_minutes"],
    )

    def clean_methods(self):
        chosen = set(self.cleaned_data["methods"])
        chosen.add("manual")  # manual is never disablable (spec §9)
        return sorted(chosen)
