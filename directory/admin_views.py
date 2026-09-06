"""Tenant-admin views for ListingType and Category configuration.

Custom views, not Django ``ModelAdmin`` -- writes go through
``directory.services`` so they emit ``tenant.settings_changed`` and hit the
command log (invariant 6). The schema builder is HTMX: rows are added and
removed by fetching a server-rendered partial, and the whole set posts as
parallel arrays.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.crypto import get_random_string

from directory import services
from directory.access import tenant_admin_required
from directory.admin_forms import CategoryForm, ListingTypeForm
from directory.field_schema import FIELD_TYPES, SchemaError, validate_type_schema
from directory.models import Category, ListingType

_ADMIN = tenant_admin_required()


def _get_type(request, key: str) -> ListingType:
    return get_object_or_404(ListingType, tenant=request.tenant, key=key)


def _parse_field_rows(post) -> list[dict]:
    keys = post.getlist("field_key")
    labels = post.getlist("field_label")
    types = post.getlist("field_type")
    options = post.getlist("field_options")
    required = post.getlist("field_required")
    public = post.getlist("field_public")
    searchable = post.getlist("field_searchable")

    def at(seq, i, default=""):
        return seq[i] if i < len(seq) else default

    rows: list[dict] = []
    for i, raw_key in enumerate(keys):
        key = (raw_key or "").strip()
        label = at(labels, i).strip()
        if not key and not label:
            continue
        ftype = at(types, i)
        row = {
            "key": key,
            "label": label,
            "type": ftype,
            "required": at(required, i) == "1",
            "public": at(public, i) == "1",
            "searchable": at(searchable, i) == "1",
        }
        if ftype in ("select", "multi_select"):
            row["options"] = [
                line.strip() for line in at(options, i).splitlines() if line.strip()
            ]
        rows.append(row)
    return rows


# --- listing types ---------------------------------------------------------


@_ADMIN
def listing_type_list(request):
    return render(
        request,
        "directory/admin/listing_type_list.html",
        {"types": ListingType.objects.all()},
    )


@_ADMIN
def listing_type_create(request):
    needs_confirm = ListingType.objects.count() == 1
    form = ListingTypeForm(
        request.POST or None, is_create=True, needs_url_confirm=needs_confirm
    )
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        try:
            listing_type = services.create_listing_type(
                request.tenant,
                key=cd["key"],
                label_singular=cd["label_singular"],
                label_plural=cd["label_plural"],
                path_segment=cd["path_segment"],
                claimable=cd["claimable"],
                fields=[],
                actor=request.user,
            )
        except (SchemaError, ValueError) as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Created “{listing_type.key}”. Now define its fields.")
            return redirect("directory_admin:type-fields", key=listing_type.key)
    return render(
        request,
        "directory/admin/listing_type_form.html",
        {"form": form, "mode": "create", "needs_confirm": needs_confirm},
    )


@_ADMIN
def listing_type_edit(request, key):
    listing_type = _get_type(request, key)
    initial = {
        "label_singular": listing_type.label_singular,
        "label_plural": listing_type.label_plural,
        "path_segment": listing_type.path_segment,
        "claimable": listing_type.claimable,
    }
    form = ListingTypeForm(request.POST or None, initial=initial, is_create=False)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        try:
            services.update_listing_type(
                listing_type,
                label_singular=cd["label_singular"],
                label_plural=cd["label_plural"],
                path_segment=cd["path_segment"],
                claimable=cd["claimable"],
                actor=request.user,
            )
        except (SchemaError, ValueError) as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Saved.")
            return redirect("directory_admin:type-list")
    return render(
        request,
        "directory/admin/listing_type_form.html",
        {"form": form, "mode": "edit", "listing_type": listing_type},
    )


@_ADMIN
def listing_type_delete(request, key):
    listing_type = _get_type(request, key)
    if request.method == "POST":
        try:
            services.delete_listing_type(listing_type, actor=request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Deleted “{key}”.")
    return redirect("directory_admin:type-list")


@_ADMIN
def schema_builder(request, key):
    listing_type = _get_type(request, key)
    errors: list[str] = []
    fields = listing_type.fields

    if request.method == "POST":
        fields = _parse_field_rows(request.POST)
        errors = validate_type_schema(fields, previous=listing_type.fields)
        if not errors:
            try:
                services.update_listing_type(
                    listing_type, fields=fields, actor=request.user
                )
            except SchemaError as exc:
                errors = exc.errors
            else:
                messages.success(request, "Field schema saved.")
                return redirect("directory_admin:type-fields", key=key)

    return render(
        request,
        "directory/admin/schema_builder.html",
        {
            "listing_type": listing_type,
            "fields": fields,
            "errors": errors,
            "field_types": sorted(FIELD_TYPES),
        },
    )


@_ADMIN
def field_row(request, key):
    _get_type(request, key)
    if request.GET.get("blank"):
        return HttpResponse("")  # HTMX row removal
    return render(
        request,
        "directory/admin/_field_row.html",
        {
            "field": {"key": "", "label": "", "type": "text", "options": []},
            "type_key": key,
            "field_types": sorted(FIELD_TYPES),
            "rid": get_random_string(6),
        },
    )


# --- categories ------------------------------------------------------------


@_ADMIN
def category_list(request, key):
    listing_type = _get_type(request, key)
    return render(
        request,
        "directory/admin/category_list.html",
        {
            "listing_type": listing_type,
            "categories": Category.objects.filter(listing_type=listing_type),
        },
    )


@_ADMIN
def category_create(request, key):
    listing_type = _get_type(request, key)
    form = CategoryForm(request.POST or None, listing_type=listing_type)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        services.create_category(
            listing_type,
            name=cd["name"],
            slug=cd["slug"],
            parent=cd["parent"],
            order=cd["order"],
            actor=request.user,
        )
        messages.success(request, f"Added category “{cd['slug']}”.")
        return redirect("directory_admin:category-list", key=key)
    return render(
        request,
        "directory/admin/category_form.html",
        {"form": form, "listing_type": listing_type, "mode": "create"},
    )


@_ADMIN
def category_edit(request, public_id):
    category = get_object_or_404(
        Category, tenant=request.tenant, public_id=public_id
    )
    initial = {
        "name": category.name,
        "slug": category.slug,
        "parent": category.parent_id,
        "order": category.order,
    }
    form = CategoryForm(
        request.POST or None,
        initial=initial,
        listing_type=category.listing_type,
        instance=category,
    )
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        services.update_category(
            category,
            name=cd["name"],
            slug=cd["slug"],
            parent=cd["parent"],
            order=cd["order"],
            actor=request.user,
        )
        messages.success(request, "Saved.")
        return redirect(
            "directory_admin:category-list", key=category.listing_type.key
        )
    return render(
        request,
        "directory/admin/category_form.html",
        {"form": form, "listing_type": category.listing_type, "mode": "edit"},
    )


@_ADMIN
def category_delete(request, public_id):
    category = get_object_or_404(
        Category, tenant=request.tenant, public_id=public_id
    )
    key = category.listing_type.key
    if request.method == "POST":
        services.delete_category(category, actor=request.user)
        messages.success(request, "Category deleted.")
    return redirect("directory_admin:category-list", key=key)
