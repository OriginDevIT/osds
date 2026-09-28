"""Public-facing forms. Distinct from directory.admin_forms (tenant-admin,
authenticated) and tenants.wizard.forms (first-run setup)."""

from __future__ import annotations

from django import forms

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

    def __init__(self, *args, enabled_methods=None, **kwargs):
        super().__init__(*args, **kwargs)
        if enabled_methods is not None:
            self.fields["method"].choices = [
                (value, label)
                for value, label in Claim.Method.choices
                if value in enabled_methods
            ]
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
