"""Forms for the operator's tier and entitlement pages (decisions.md §4.11)."""

from __future__ import annotations

import json

from django import forms

from billing.models import Entitlement, Tier

_DT = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
_DT_FORMATS = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"]


def _datetime(label, *, required=False, help_text=""):
    return forms.DateTimeField(
        label=label, required=required, widget=_DT, input_formats=_DT_FORMATS,
        help_text=help_text,
    )


class TierForm(forms.Form):
    key = forms.SlugField(max_length=50)
    name = forms.CharField(max_length=100)
    rank = forms.IntegerField(
        min_value=0, help_text="0 is the free fallback tier. Higher ranks sort higher."
    )
    purchasable = forms.BooleanField(required=False)
    price_minor = forms.IntegerField(
        required=False, min_value=1, label="Price (minor units)",
        help_text="Whole minor units: 1900 with USD is $19.00.",
    )
    currency = forms.CharField(
        required=False, max_length=3, help_text="Three-letter ISO 4217 code, such as USD."
    )
    interval = forms.ChoiceField(
        required=False, choices=[("", "—"), *Tier.Interval.choices]
    )
    trial_days = forms.IntegerField(
        required=False, min_value=1, max_value=90,
        help_text="A card-up-front trial. Leave blank for none.",
    )
    badge_label = forms.CharField(required=False, max_length=50)
    perks = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 4}),
        help_text='A JSON object, for example {"priority": true}. Not interpreted yet.',
    )

    def __init__(self, *args, editing: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if editing:
            self.fields["key"].disabled = True

    def clean_perks(self):
        raw = (self.cleaned_data.get("perks") or "").strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise forms.ValidationError("Perks must be valid JSON.") from exc
        if not isinstance(value, dict):
            raise forms.ValidationError("Perks must be a JSON object.")
        return value

    def clean_currency(self):
        return (self.cleaned_data.get("currency") or "").upper()

    def tier_kwargs(self) -> dict:
        cd = self.cleaned_data
        return {
            "name": cd["name"], "rank": cd["rank"], "purchasable": cd["purchasable"],
            "price_minor": cd.get("price_minor"), "currency": cd.get("currency", ""),
            "interval": cd.get("interval", ""), "trial_days": cd.get("trial_days"),
            "badge_label": cd.get("badge_label", ""), "perks": cd.get("perks", {}),
        }


class GrantCompForm(forms.Form):
    listing = forms.CharField(
        label="Listing id", help_text="The listing's public id, such as listing_01…."
    )
    tier = forms.ChoiceField(choices=())
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))
    expires_at = _datetime("Expires", help_text="Leave blank to run indefinitely.")

    def __init__(self, *args, tiers, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tier"].choices = [(t.key, f"{t.name} (rank {t.rank})") for t in tiers]


class RevokeForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))


class OverrideForm(forms.Form):
    to_status = forms.ChoiceField(
        choices=[(v, l) for v, l in Entitlement.Status.choices if v != Entitlement.Status.COMPED]
    )
    tier = forms.ChoiceField(required=False, choices=())
    period_end = _datetime("Period end", help_text="Needed for cancelled.")
    trial_ends_at = _datetime("Trial ends", help_text="Needed for trialing.")
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, tiers, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tier"].choices = [("", "— keep —")] + [
            (t.key, f"{t.name} (rank {t.rank})") for t in tiers
        ]
