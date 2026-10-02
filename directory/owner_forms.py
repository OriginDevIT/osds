"""The owner's edit form: only what an owner may change (decisions.md §4.9).

Description, phone and the listing type's public custom fields. Name, slug,
categories, website, email and address are not fields here, so there is
nothing to tamper with; a crafted POST that names one is refused by the view
(``directory.owner_edit``), not ignored.
"""

from __future__ import annotations

from django import forms

from directory.admin_forms import _custom_form_field
from directory.owner_edit import editable_custom_keys


def build_owner_edit_form_class(listing_type):
    attrs: dict = {
        "description": forms.CharField(
            widget=forms.Textarea(attrs={"rows": 6}), required=False,
            label="Description",
        ),
        "phone_e164": forms.CharField(
            max_length=16, required=False, label="Phone",
            help_text="Include the country code, for example +17735550142.",
        ),
    }
    cf_specs: list[tuple[str, str]] = []
    editable = editable_custom_keys(listing_type)
    for descriptor in listing_type.fields:
        if descriptor["key"] not in editable:
            continue
        name = f"cf_{descriptor['key']}"
        attrs[name] = _custom_form_field(descriptor)
        cf_specs.append((descriptor["key"], name))
    attrs["cf_specs"] = cf_specs
    return type("OwnerEditForm", (forms.Form,), attrs)


def initial_from_listing(listing, form_class) -> dict:
    initial = {
        "description": listing.description,
        "phone_e164": listing.phone_e164,
    }
    for key, name in form_class.cf_specs:
        initial[name] = (listing.custom_fields or {}).get(key)
    return initial


def changes_from_form(form) -> dict:
    """Every free field, always, so a cleared input is an explicit ``None``
    (spec §7.1: a UI that omits empty inputs can never clear a field)."""
    cd = form.cleaned_data

    def blank_to_none(value):
        return value if value not in ("", None, []) else None

    return {
        "description": blank_to_none(cd.get("description")),
        "phone_e164": blank_to_none(cd.get("phone_e164")),
        "custom_fields": {
            key: blank_to_none(cd.get(name)) for key, name in form.cf_specs
        },
    }


class OwnerMediaForm(forms.Form):
    ROLE_CHOICES = [
        ("gallery", "Gallery image"),
        ("logo", "Logo"),
        ("cover", "Cover image"),
    ]
    role = forms.ChoiceField(choices=ROLE_CHOICES)
    image = forms.FileField()
    alt_text = forms.CharField(max_length=255, required=False, label="Alt text")
