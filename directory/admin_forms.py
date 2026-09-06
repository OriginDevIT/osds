from __future__ import annotations

from django import forms

from directory.models import Category, Listing


class ListingTypeForm(forms.Form):
    """Listing-type metadata. The field schema is edited separately in the
    schema builder. ``key`` appears only on create -- it is frozen afterwards
    (ruling 6)."""

    label_singular = forms.CharField(max_length=100)
    label_plural = forms.CharField(max_length=100)
    path_segment = forms.SlugField(
        max_length=50, help_text="URL segment, used once a tenant has more than one type."
    )
    claimable = forms.BooleanField(required=False, initial=True)

    def __init__(self, *args, is_create: bool = True, needs_url_confirm: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if is_create:
            self.fields["key"] = forms.SlugField(
                max_length=50, help_text="Internal identifier. Cannot be changed later."
            )
            self.order_fields(["key", "label_singular", "label_plural", "path_segment", "claimable"])
        if needs_url_confirm:
            self.fields["confirm_url_change"] = forms.BooleanField(
                required=True,
                label=(
                    "I understand that adding a second type moves the existing "
                    "listing URLs under a segment and issues permanent redirects."
                ),
            )


class CategoryForm(forms.Form):
    name = forms.CharField(max_length=200)
    slug = forms.SlugField(max_length=100)
    # Placeholder; the real, tenant-scoped queryset is set in __init__.
    parent = forms.ModelChoiceField(
        queryset=Category.all_tenants.none(), required=False
    )
    order = forms.IntegerField(min_value=0, initial=0)

    def __init__(self, *args, listing_type=None, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Category.objects.filter(listing_type=listing_type)
        if instance is not None:
            qs = qs.exclude(pk=instance.pk)
        self.fields["parent"].queryset = qs


# --- listing form (built per listing type from its field schema) ------------

_CORE_FIELDS = {
    "name": lambda: forms.CharField(max_length=255),
    "slug": lambda: forms.SlugField(max_length=200),
    "description": lambda: forms.CharField(widget=forms.Textarea, required=False),
    "address_line1": lambda: forms.CharField(max_length=255, required=False),
    "address_line2": lambda: forms.CharField(max_length=255, required=False),
    "locality": lambda: forms.CharField(max_length=120, required=False),
    "region": lambda: forms.CharField(max_length=120, required=False),
    "postal_code": lambda: forms.CharField(max_length=20, required=False),
    "country": lambda: forms.CharField(max_length=2, required=False),
    "lat": lambda: forms.DecimalField(max_digits=9, decimal_places=6, required=False),
    "lon": lambda: forms.DecimalField(max_digits=9, decimal_places=6, required=False),
    "phone_e164": lambda: forms.CharField(max_length=16, required=False),
    "email": lambda: forms.EmailField(required=False),
    "website": lambda: forms.URLField(required=False),
}


def _custom_form_field(descriptor: dict):
    ftype = descriptor["type"]
    label = descriptor["label"]
    required = bool(descriptor.get("required"))
    options = [(o, o) for o in descriptor.get("options", [])]
    kw = {"label": label, "required": required}

    builders = {
        "text": lambda: forms.CharField(**kw),
        "long_text": lambda: forms.CharField(widget=forms.Textarea, **kw),
        "integer": lambda: forms.IntegerField(**kw),
        "decimal": lambda: forms.DecimalField(**kw),
        "boolean": lambda: forms.BooleanField(label=label, required=False),
        "date": lambda: forms.DateField(**kw),
        "url": lambda: forms.URLField(**kw),
        "email": lambda: forms.EmailField(**kw),
        "select": lambda: forms.ChoiceField(choices=[("", "—"), *options], **kw),
        "multi_select": lambda: forms.MultipleChoiceField(
            choices=options, label=label, required=False
        ),
    }
    return builders[ftype]()


def build_listing_form_class(listing_type):
    """A Form class for one listing type: the common core plus a ``cf_<key>``
    field per schema descriptor."""
    attrs: dict = {name: build() for name, build in _CORE_FIELDS.items()}
    attrs["geo_precision"] = forms.ChoiceField(
        choices=[("", "—"), *Listing.GeoPrecision.choices], required=False
    )
    attrs["categories"] = forms.ModelMultipleChoiceField(
        queryset=Category.all_tenants.none(), required=False
    )

    cf_specs: list[tuple[str, str]] = []
    for descriptor in listing_type.fields:
        field_name = f"cf_{descriptor['key']}"
        attrs[field_name] = _custom_form_field(descriptor)
        cf_specs.append((descriptor["key"], field_name))

    def __init__(self, *args, **kwargs):
        forms.Form.__init__(self, *args, **kwargs)
        self.fields["categories"].queryset = Category.objects.filter(
            listing_type=listing_type
        )

    attrs["__init__"] = __init__
    attrs["cf_specs"] = cf_specs
    return type("ListingForm", (forms.Form,), attrs)
