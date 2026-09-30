"""Public-facing forms. Distinct from directory.admin_forms (tenant-admin,
authenticated) and tenants.wizard.forms (first-run setup)."""

from __future__ import annotations

from django import forms
from django.core.validators import RegexValidator

from directory.claims import domain_email_host
from directory.models import Claim
from directory.services import CONSENT_CHANNELS

_CONSENT_LABELS = {
    "marketing_email": "Email me about this listing",
    "marketing_sms": "Text me about this listing",
    "automated_calls": "Call me with automated messages about this listing",
}


class ClaimForm(forms.Form):
    name = forms.CharField(max_length=200)
    email = forms.EmailField()
    phone_e164 = forms.CharField(max_length=16, required=False)
    role_claimed = forms.CharField(
        max_length=40, initial="owner", widget=forms.HiddenInput
    )
    method = forms.ChoiceField(choices=Claim.Method.choices)

    def __init__(self, *args, listing=None, enabled_methods=None, **kwargs):
        super().__init__(*args, **kwargs)
        # domain_email needs a listing to check eligibility against (spec
        # §9.6, decisions.md §4.4: "ClaimForm offers domain_email only for
        # an eligible listing"), on top of whether the tenant enabled it.
        self.domain_email_host = domain_email_host(listing) if listing else None
        if enabled_methods is not None or self.domain_email_host is None:
            allowed = set(enabled_methods) if enabled_methods is not None else {
                v for v, _ in Claim.Method.choices
            }
            if self.domain_email_host is None:
                allowed.discard(Claim.Method.DOMAIN_EMAIL)
            self.fields["method"].choices = [
                (value, label)
                for value, label in Claim.Method.choices
                if value in allowed
            ]
        if self.domain_email_host:
            self.fields["method"].help_text = (
                f"Domain email requires an address at @{self.domain_email_host}."
            )
        for channel in CONSENT_CHANNELS:
            self.fields[channel] = forms.BooleanField(
                required=False, label=_CONSENT_LABELS[channel]
            )

    def consent_payload(self) -> dict:
        """Every channel is present regardless of whether it was checked
        (spec §9.0) -- an unchecked box is an explicit decline, not an
        omission."""
        return {
            channel: {"granted": self.cleaned_data.get(channel, False)}
            for channel in CONSENT_CHANNELS
        }


class ClaimVerifyForm(forms.Form):
    # A letter typo (or any non-digit) is a form error, not a wrong-code
    # attempt -- it never reaches verify_claim_code, so it consumes no
    # attempt on the guard row.
    code = forms.CharField(
        label="Verification code",
        validators=[RegexValidator(r"^\d{6}$", "Enter the 6-digit code.")],
    )
