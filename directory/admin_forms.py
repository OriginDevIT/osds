from __future__ import annotations

from django import forms

from directory.models import Category


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
