from __future__ import annotations

import re

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from tenants.models import Tenant

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
        label="Domain", help_text="The hostname the directory will be served on."
    )

    def clean_domain(self):
        domain = self.cleaned_data["domain"].strip().rstrip(".").lower()
        if not _HOSTNAME_RE.match(domain):
            raise ValidationError("Enter a valid domain, e.g. directory.example.com.")
        return domain


class StorageForm(forms.Form):
    BACKENDS = [
        ("local", "Local disk"),
        ("s3", "S3-compatible"),
        ("azure", "Azure Blob Storage"),
        ("gcp", "Google Cloud Storage"),
    ]
    backend = forms.ChoiceField(label="Storage backend", choices=BACKENDS)
    bucket = forms.CharField(label="Bucket / container", required=False)
    endpoint = forms.CharField(label="Endpoint URL", required=False)
    access_key = forms.CharField(label="Access key", required=False)
    secret_key = forms.CharField(
        label="Secret key", required=False, widget=forms.PasswordInput(render_value=False)
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("backend") != "local" and not cleaned.get("bucket"):
            self.add_error("bucket", "Required for a cloud backend.")
        return cleaned


class SmtpForm(forms.Form):
    host = forms.CharField(label="SMTP host")
    port = forms.IntegerField(label="Port", min_value=1, max_value=65535, initial=587)
    from_email = forms.EmailField(label="From address")
    username = forms.CharField(label="Username", required=False)
    password = forms.CharField(
        label="Password", required=False, widget=forms.PasswordInput(render_value=False)
    )
    use_tls = forms.BooleanField(label="Use STARTTLS", required=False, initial=True)


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
    domain_email_ttl_minutes = forms.IntegerField(
        label="Domain-email code lifetime (minutes)",
        min_value=15,
        max_value=2880,
        initial=1440,
    )

    def clean_methods(self):
        chosen = set(self.cleaned_data["methods"])
        chosen.add("manual")  # manual is never disablable (spec §9)
        return sorted(chosen)
