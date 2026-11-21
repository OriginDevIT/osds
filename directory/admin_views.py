"""Tenant-admin views for ListingType and Category configuration.

Custom views, not Django ``ModelAdmin`` -- writes go through
``directory.services`` so they emit ``tenant.settings_changed`` and hit the
command log (invariant 6). The schema builder is HTMX: rows are added and
removed by fetching a server-rendered partial, and the whole set posts as
parallel arrays.
"""

from __future__ import annotations

import json

from django.contrib import messages
from django.core.files.base import ContentFile
from django.db import IntegrityError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.crypto import get_random_string

from audit.command_log import log_conclude, log_received, require_autocommit
from directory import csv_import, importing, media, normalize, services
from directory.access import tenant_admin_required
from directory.admin_forms import (
    CategoryForm,
    ImportUploadForm,
    ListingTypeForm,
    MediaUploadForm,
    build_listing_form_class,
)
from directory.field_schema import (
    CORE_FIELDS,
    FIELD_TYPES,
    SchemaError,
    near_duplicate_warnings,
    validate_type_schema,
)
from directory.models import (
    Category,
    ImportBatch,
    ImportBatchListing,
    Listing,
    ListingType,
    MediaAsset,
    PathRedirect,
)
from directory.storage import StorageConfigError, get_tenant_storage
from tenants.models import StaffMembership

_ADMIN = tenant_admin_required()
_MANAGER = tenant_admin_required(StaffMembership.Role.MANAGER)
_EDITOR = tenant_admin_required(StaffMembership.Role.EDITOR)


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


def _category_descendant_ids(category: Category) -> list[int]:
    """Ids of every category beneath ``category`` (CASCADE is recursive)."""
    found: list[int] = []
    frontier = [category.pk]
    while frontier:
        frontier = list(
            Category.objects.filter(
                tenant=category.tenant, parent_id__in=frontier
            ).values_list("pk", flat=True)
        )
        found.extend(frontier)
    return found


def _association_count(tenant, category_ids: list[int]) -> int:
    through = Listing.categories.through
    return through.objects.filter(
        category_id__in=category_ids, listing__tenant=tenant
    ).count()


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
    category_ids = list(
        Category.objects.filter(
            tenant=request.tenant, listing_type=listing_type
        ).values_list("pk", flat=True)
    )
    return render(
        request,
        "directory/admin/listing_type_confirm_delete.html",
        {
            "listing_type": listing_type,
            "category_count": len(category_ids),
            "association_count": _association_count(request.tenant, category_ids),
            "listing_count": Listing.objects.filter(
                tenant=request.tenant, listing_type=listing_type
            ).count(),
        },
    )


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
            "core_fields": CORE_FIELDS,
            "warnings": near_duplicate_warnings(fields),
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
    child_ids = _category_descendant_ids(category)
    return render(
        request,
        "directory/admin/category_confirm_delete.html",
        {
            "category": category,
            "listing_type": category.listing_type,
            "child_count": len(child_ids),
            "association_count": _association_count(
                request.tenant, [category.pk, *child_ids]
            ),
        },
    )


# --- listings (editor rank: editing listing content, spec §4.4) ------------

_CORE_KEYS = (
    "address_line1",
    "address_line2",
    "locality",
    "region",
    "postal_code",
    "country",
)


def _actor(request) -> dict:
    return {"type": "admin", "id": request.user.public_id}


def _payload_from_form(form) -> dict:
    cd = form.cleaned_data

    def blank_to_none(value):
        return value if value not in ("", None, []) else None

    payload = {
        "slug": cd["slug"],
        "name": cd["name"],
        "description": blank_to_none(cd.get("description")),
        "location": {k: blank_to_none(cd.get(k)) for k in _CORE_KEYS},
        "contact": {
            "phone_e164": blank_to_none(cd.get("phone_e164")),
            "email": blank_to_none(cd.get("email")),
            "website": blank_to_none(cd.get("website")),
        },
        "categories": sorted(c.slug for c in cd.get("categories", [])),
        "custom_fields": {},
    }

    lat, lon = cd.get("lat"), cd.get("lon")
    payload["location"]["lat"] = lat
    payload["location"]["lon"] = lon
    if (lat is not None or lon is not None) and cd.get("geo_precision"):
        payload["location"]["geo_precision"] = cd["geo_precision"]

    for schema_key, field_name in form.cf_specs:
        payload["custom_fields"][schema_key] = blank_to_none(cd.get(field_name))
    return payload


def _initial_from_listing(listing, listing_type) -> dict:
    initial = {
        "name": listing.name,
        "slug": listing.slug,
        "description": listing.description,
        "lat": listing.lat,
        "lon": listing.lon,
        "geo_precision": listing.geo_precision,
        "phone_e164": listing.phone_e164,
        "email": listing.email,
        "website": listing.website,
        "categories": list(listing.categories.all()),
    }
    for key in _CORE_KEYS:
        initial[key] = getattr(listing, key)
    for descriptor in listing_type.fields:
        initial[f"cf_{descriptor['key']}"] = (listing.custom_fields or {}).get(
            descriptor["key"]
        )
    return initial


def _form_errors(form, exc) -> None:
    if isinstance(exc, services.RejectedField):
        form.add_error(None, str(exc))
    else:
        for message in exc.errors:
            form.add_error(None, message)


@_EDITOR
def listing_list(request, key):
    listing_type = _get_type(request, key)
    return render(
        request,
        "directory/admin/listing_list.html",
        {
            "listing_type": listing_type,
            "listings": Listing.objects.filter(
                listing_type=listing_type
            ).order_by("-created_at"),
        },
    )


@_EDITOR
def listing_create(request, key):
    listing_type = _get_type(request, key)
    form_class = build_listing_form_class(listing_type)
    form = form_class(request.POST or None)
    conflict = None

    if request.method == "POST" and form.is_valid():
        try:
            result = services.upsert_listing(
                request.tenant,
                listing_type=listing_type,
                payload=_payload_from_form(form),
                actor=_actor(request),
                source="manual",
                must_create=True,
            )
        except services.RejectedField as exc:
            _form_errors(form, exc)
        except SchemaError as exc:
            if any("already exists" in e for e in exc.errors):
                conflict = Listing.objects.filter(
                    tenant=request.tenant,
                    listing_type=listing_type,
                    slug=normalize.slug(form.cleaned_data["slug"]),
                ).first()
            else:
                _form_errors(form, exc)
        else:
            messages.success(request, "Listing created.")
            return redirect(
                "directory_admin:listing-edit",
                key=key,
                public_id=result.listing.public_id,
            )

    return render(
        request,
        "directory/admin/listing_form.html",
        {"form": form, "listing_type": listing_type, "mode": "create", "conflict": conflict},
    )


@_EDITOR
def listing_edit(request, key, public_id):
    listing_type = _get_type(request, key)
    listing = get_object_or_404(
        Listing,
        tenant=request.tenant,
        listing_type=listing_type,
        public_id=public_id,
    )
    form_class = build_listing_form_class(listing_type)

    if request.method == "POST":
        form = form_class(request.POST)
        if form.is_valid():
            try:
                result = services.upsert_listing(
                    request.tenant,
                    listing_type=listing_type,
                    payload={**_payload_from_form(form), "id": listing.public_id},
                    actor=_actor(request),
                    source="manual",
                )
            except (services.RejectedField, SchemaError) as exc:
                _form_errors(form, exc)
            else:
                messages.success(
                    request,
                    "No changes." if result.outcome == "unchanged" else "Saved.",
                )
                return redirect(
                    "directory_admin:listing-edit", key=key, public_id=public_id
                )
    else:
        form = form_class(initial=_initial_from_listing(listing, listing_type))

    return render(
        request,
        "directory/admin/listing_form.html",
        {
            "form": form,
            "listing_type": listing_type,
            "mode": "edit",
            "listing": listing,
            "media_form": MediaUploadForm(),
            "media_assets": MediaAsset.objects.filter(listing=listing).order_by(
                "role", "sort_order", "id"
            ),
        },
    )


def _set_visibility(request, key, public_id, visibility, verb):
    listing_type = _get_type(request, key)
    listing = get_object_or_404(
        Listing,
        tenant=request.tenant,
        listing_type=listing_type,
        public_id=public_id,
    )
    if request.method == "POST":
        services.set_listing_visibility(
            listing, visibility, actor=_actor(request)
        )
        messages.success(request, f"{verb}.")
    return redirect("directory_admin:listing-edit", key=key, public_id=public_id)


@_EDITOR
def listing_publish(request, key, public_id):
    return _set_visibility(
        request, key, public_id, Listing.Visibility.PUBLISHED, "Published"
    )


@_EDITOR
def listing_unpublish(request, key, public_id):
    return _set_visibility(
        request, key, public_id, Listing.Visibility.DRAFT, "Unpublished"
    )


def _get_listing(request, key, public_id) -> Listing:
    return get_object_or_404(
        Listing,
        tenant=request.tenant,
        listing_type=_get_type(request, key),
        public_id=public_id,
    )


@_EDITOR
def listing_media_add(request, key, public_id):
    listing = _get_listing(request, key, public_id)
    if request.method == "POST":
        form = MediaUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            messages.error(request, "Choose an image and a placement.")
        else:
            try:
                media.attach_media(
                    listing,
                    role=form.cleaned_data["role"],
                    upload=form.cleaned_data["image"],
                    actor=_actor(request),
                    alt_text=form.cleaned_data["alt_text"],
                    uploaded_by=request.user,
                )
            except (media.MediaError, StorageConfigError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Image added.")
    return redirect(
        "directory_admin:listing-edit", key=key, public_id=public_id
    )


@_EDITOR
def listing_media_thumbnail(request, key, public_id, asset_public_id):
    """Stream one ready asset of a listing to a signed-in editor.

    The public ``/media/<id>`` view only serves published listings, so the edit
    page cannot use it for a draft. This path is gated by the same role as the
    edit page, is scoped to the tenant and to the listing in the URL, and is
    not visibility-gated. It is marked ``private`` so shared caches never keep
    a copy.
    """
    listing = _get_listing(request, key, public_id)
    asset = get_object_or_404(
        MediaAsset,
        tenant=request.tenant,
        listing=listing,
        public_id=asset_public_id,
        status=MediaAsset.Status.READY,
    )
    try:
        handle = get_tenant_storage(request.tenant).open(asset.storage_key)
    except FileNotFoundError as exc:
        raise Http404 from exc
    response = FileResponse(handle, content_type=asset.content_type or None)
    response["Cache-Control"] = "private, max-age=300"
    return response


@_EDITOR
def listing_media_remove(request, key, public_id, asset_public_id):
    listing = _get_listing(request, key, public_id)
    asset = get_object_or_404(
        MediaAsset,
        tenant=request.tenant,
        listing=listing,
        public_id=asset_public_id,
    )
    if request.method == "POST":
        try:
            media.detach_media(asset, actor=_actor(request))
        except StorageConfigError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Image removed.")
    return redirect(
        "directory_admin:listing-edit", key=key, public_id=public_id
    )


# --- CSV import: upload and column mapping (spec §4.1.1, §7.1) -------------
#
# The upload writes one ImportBatch row and a matching import.upload
# command-log pair (§11.2); the file lands in tenant storage. "Run import"
# moves the batch to `pending`; the worker picks it up from there
# (directory/importing.py), so no row processing happens in a request.


@_EDITOR
def import_list(request):
    return render(
        request,
        "directory/admin/import_list.html",
        {"batches": ImportBatch.objects.order_by("-id")},
    )


def _store_upload(request, *, upload, raw, listing_type, headers, delimiter, encoding):
    """Write the ImportBatch row and the stored file, bracketed by an
    import.upload command-log pair. Runs in autocommit (§11.2)."""
    require_autocommit()
    row = log_received(
        command="import.upload",
        tenant=request.tenant,
        idempotency_key=None,
        actor=_actor(request),
        trace_id=None,
        origin="",
        payload={
            "filename": (upload.name or "")[:255],
            "listing_type": listing_type.key,
            "bytes": len(raw),
            "delimiter": delimiter,
            "encoding": encoding,
            "headers": headers,
            "started_by": request.user.public_id,
        },
    )
    try:
        storage = get_tenant_storage(request.tenant)
    except StorageConfigError as exc:
        log_conclude(row, outcome="rejected", problem={"storage": str(exc)})
        raise

    # Any failure past here leaves the row unconcluded -- the "threw mid-apply"
    # record (§11.2).
    batch = ImportBatch(
        tenant=request.tenant,
        listing_type=listing_type,
        source="csv",
        status=ImportBatch.Status.MAPPING,
        original_filename=(upload.name or "")[:255],
        detected_headers=headers,
        delimiter=delimiter,
        encoding=encoding,
        has_header=True,
        started_by=request.user,
    )
    batch.stored_path = storage.save(
        f"imports/{batch.public_id}.csv", ContentFile(raw)
    )
    batch.save()

    log_conclude(row, outcome="applied")
    return batch


def _upload_data(request):
    """POST data for the upload form. Before confirmation a blank key (and
    then a blank segment) is filled with the proposed default so the operator
    sees it and confirms it; at confirmation nothing is filled in."""
    if request.method != "POST":
        return None
    data = request.POST.copy()
    if data.get("type_mode") == "new" and data.get("action") != "confirm":
        if not data.get("new_key", "").strip():
            data["new_key"] = csv_import.derive_key(data.get("new_label_singular", ""))
        if not data.get("new_path_segment", "").strip():
            data["new_path_segment"] = data["new_key"]
    return data


def _posted_proposal(post, headers):
    """The operator's edited proposal rows (included ones only, plus every row
    for redisplay) from the confirmation POST."""
    rows = []
    for raw in post.getlist("col"):
        if not raw.isdigit() or int(raw) >= len(headers):
            continue
        i = int(raw)
        ftype = post.get(f"type__{i}", "text")
        rows.append(
            {
                "index": i,
                "header": headers[i],
                "key": post.get(f"key__{i}", "").strip(),
                "label": post.get(f"label__{i}", "").strip(),
                "type": ftype,
                "include": post.get(f"inc__{i}") == "1",
            }
        )
    return rows


def _create_type_and_store(request, form, fields, **upload_kwargs):
    """Create the new type through the service, then the batch against it.

    ``create_listing_type`` and ``_store_upload`` each need autocommit, so the
    pair cannot share a transaction. Storage is resolved first so the common
    failure leaves nothing behind; a failure after the type exists removes it
    (and the redirect that creating it recorded) again."""
    get_tenant_storage(request.tenant)  # StorageConfigError before any write
    cd = form.cleaned_data
    had_redirect = PathRedirect.objects.filter(old_prefix="").exists()
    listing_type = services.create_listing_type(
        request.tenant,
        key=cd["new_key"],
        label_singular=cd["new_label_singular"],
        label_plural=cd["new_label_plural"],
        path_segment=cd["new_path_segment"],
        claimable=cd["new_claimable"],
        fields=fields,
        actor=request.user,
    )
    try:
        return _store_upload(request, listing_type=listing_type, **upload_kwargs)
    except BaseException:
        services.delete_listing_type(listing_type, actor=request.user)
        if not had_redirect:
            PathRedirect.objects.filter(old_prefix="").delete()
        raise


@_EDITOR
def import_create(request):
    form = ImportUploadForm(_upload_data(request), request.FILES or None)
    context = {"form": form}
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        raw = upload.read()
        headers, delimiter, encoding = csv_import.read_header(raw)
        if not headers:
            form.add_error("file", "No header row found in the file.")
        elif len(headers) > csv_import.HEADER_LIMIT:
            form.add_error(
                "file",
                f"{len(headers)} columns detected; the limit is "
                f"{csv_import.HEADER_LIMIT}.",
            )
        elif form.creates_type:
            return _import_new_type(
                request, form, context,
                upload=upload, raw=raw, headers=headers,
                delimiter=delimiter, encoding=encoding,
            )
        else:
            try:
                batch = _store_upload(
                    request,
                    upload=upload,
                    raw=raw,
                    listing_type=form.cleaned_data["listing_type"],
                    headers=headers,
                    delimiter=delimiter,
                    encoding=encoding,
                )
            except StorageConfigError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request, "Uploaded. Map the columns, then run the import."
                )
                return redirect(
                    "directory_admin:import-detail", public_id=batch.public_id
                )
    return render(request, "directory/admin/import_form.html", context)


def _import_new_type(request, form, context, *, upload, raw, headers, delimiter, encoding):
    """Two steps. Without ``action=confirm`` the file's header is read and the
    proposed schema is shown; nothing is written. On confirmation the edited
    proposal is validated and, only if clean, the type and the batch are
    created."""
    confirming = request.POST.get("action") == "confirm"
    context["proposal_headers"] = json.dumps(headers)
    if not confirming:
        context["proposal"] = [
            {**r, "include": True, "errors": []}
            for r in csv_import.propose_schema(headers)
        ]
        context["proposal_types"] = csv_import.PROPOSAL_TYPES
        return render(request, "directory/admin/import_form.html", context)

    # The file input is empty after the preview round-trip, so the operator
    # chooses it again; the proposal belongs to this exact header row.
    if request.POST.get("proposal_headers") != json.dumps(headers):
        form.add_error(
            "file",
            "This file's columns differ from the file that was previewed. "
            "Choose the same file again, or preview this one first.",
        )
        return render(request, "directory/admin/import_form.html", context)

    posted = _posted_proposal(request.POST, headers)
    included = [r for r in posted if r["include"]]
    row_errors = csv_import.check_proposal(included)
    by_index = {r["index"]: e for r, e in zip(included, row_errors)}
    context["proposal"] = [
        {**r, "errors": by_index.get(r["index"], [])} for r in posted
    ]
    context["proposal_types"] = csv_import.PROPOSAL_TYPES
    if any(row_errors):
        messages.error(request, "Fix the highlighted fields; nothing was created.")
        return render(request, "directory/admin/import_form.html", context)

    try:
        batch = _create_type_and_store(
            request,
            form,
            csv_import.schema_from_proposal(included),
            upload=upload,
            raw=raw,
            headers=headers,
            delimiter=delimiter,
            encoding=encoding,
        )
    except StorageConfigError as exc:
        form.add_error(None, str(exc))
    except (SchemaError, ValueError, IntegrityError) as exc:
        form.add_error(None, str(exc))
    else:
        messages.success(
            request,
            "Created the listing type and uploaded the file. "
            "Map the columns, then run the import.",
        )
        return redirect("directory_admin:import-detail", public_id=batch.public_id)
    return render(request, "directory/admin/import_form.html", context)


def _get_batch(request, public_id) -> ImportBatch:
    return get_object_or_404(
        ImportBatch, tenant=request.tenant, public_id=public_id
    )


def _import_preview(batch) -> "list[dict]":
    """The first rows after phone and multi-value normalisation, using the
    saved mapping and the batch's own settings -- the same ``build_payload``
    the worker runs. A row that would fail shows its error."""
    headers = batch.detected_headers or []
    mapping = batch.column_mapping or {}
    options = {
        f"custom_fields.{f['key']}": f.get("options", [])
        for f in batch.listing_type.fields or []
        if f.get("type") == "multi_select"
    }
    out = []
    for n, cells in enumerate(importing.preview_rows(batch), start=1):
        try:
            payload = csv_import.build_payload(
                cells, headers, mapping,
                listing_type=batch.listing_type,
                phone_country=batch.phone_default_country,
                separators=batch.column_separators,
            )
        except SchemaError as exc:
            out.append({"n": n, "error": "; ".join(exc.errors), "items": []})
            continue
        items = []
        phone = (payload.get("contact") or {}).get("phone_e164")
        if phone:
            items.append(("Phone", phone))
        if "categories" in payload:
            items.append(("Categories", " · ".join(payload["categories"])))
        errors = []
        for key, values in (payload.get("custom_fields") or {}).items():
            if isinstance(values, list):
                items.append((key, " · ".join(values)))
                bad = [v for v in values if v not in options.get(f"custom_fields.{key}", [])]
                if bad:
                    errors.append(f"{key}: not an option: {', '.join(bad)}")
        out.append({"n": n, "items": items, "error": "; ".join(errors)})
    return out


@_EDITOR
def import_detail(request, public_id):
    batch = _get_batch(request, public_id)
    mapping = batch.column_mapping or {}
    headers = batch.detected_headers or []
    can_map = batch.status == ImportBatch.Status.MAPPING
    # Suggestions only pre-select an unsaved form; a saved mapping is the
    # operator's own and is shown as-is.
    suggestions = (
        csv_import.suggest_mapping(headers, batch.listing_type)
        if can_map and not mapping
        else {}
    )
    multi_targets = csv_import.multi_value_targets(batch.listing_type)
    separators = batch.column_separators or {}
    rows = [
        {
            "index": i,
            "header": h,
            "selected": mapping.get(h) or suggestions.get(h, ""),
            "suggested": h in suggestions,
            "separator": separators.get(h, csv_import.DEFAULT_SEPARATOR),
        }
        for i, h in enumerate(headers)
    ]
    terminal = batch.status in (
        ImportBatch.Status.COMPLETED,
        ImportBatch.Status.FAILED,
    )
    window_passed = terminal and ImportBatchListing.objects.filter(
        batch=batch, pre_image_nulled_at__isnull=False
    ).exists()
    # import_rollback is gated at manager (@_MANAGER below) -- the button must
    # not render for a rank the view itself would 403.
    rollbackable = terminal and not window_passed
    has_role = request.membership.role >= StaffMembership.Role.MANAGER
    can_rollback = rollbackable and has_role
    return render(
        request,
        "directory/admin/import_detail.html",
        {
            "batch": batch,
            "rows": rows,
            "targets": csv_import.allowed_targets(batch.listing_type),
            "multi_targets": sorted(multi_targets),
            "phone_countries": sorted(normalize.PHONE_COUNTRIES),
            "preview": _import_preview(batch) if can_map and mapping else [],
            "can_map": can_map,
            "can_rollback": can_rollback,
            "rollback_window_passed": window_passed,
            # The batch itself is rollbackable; only the viewer's rank is short.
            "rollback_needs_role": rollbackable and not has_role,
        },
    )


@_MANAGER
def import_rollback(request, public_id):
    batch = _get_batch(request, public_id)
    if request.method == "POST":
        try:
            services.rollback_import_batch(batch, operator=request.user)
        except services.RollbackRefused as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, "Import rolled back.")
    return redirect("directory_admin:import-detail", public_id=public_id)


@_EDITOR
def import_mapping(request, public_id):
    batch = _get_batch(request, public_id)
    if request.method == "POST" and batch.status == ImportBatch.Status.MAPPING:
        submitted = {
            header: request.POST.get(f"map__{i}", "")
            for i, header in enumerate(batch.detected_headers or [])
        }
        try:
            cleaned = csv_import.validate_mapping(
                submitted,
                headers=batch.detected_headers or [],
                listing_type=batch.listing_type,
            )
            separators = csv_import.clean_separators(
                {
                    header: request.POST.get(f"sep__{i}", "")
                    for i, header in enumerate(batch.detected_headers or [])
                },
                cleaned,
                batch.listing_type,
            )
            phone_country = csv_import.validate_phone_country(
                request.POST.get("phone_country", "")
            )
        except csv_import.MappingError as exc:
            messages.error(request, str(exc))
        else:
            batch.column_mapping = cleaned
            batch.column_separators = separators
            batch.phone_default_country = phone_country
            batch.save(
                update_fields=[
                    "column_mapping", "column_separators", "phone_default_country",
                ]
            )
            messages.success(request, "Column mapping saved.")
    return redirect("directory_admin:import-detail", public_id=public_id)


@_EDITOR
def import_run(request, public_id):
    batch = _get_batch(request, public_id)
    if request.method == "POST" and batch.status == ImportBatch.Status.MAPPING:
        if not batch.column_mapping:
            messages.error(request, "Save a column mapping first.")
        else:
            batch.status = ImportBatch.Status.PENDING
            batch.save(update_fields=["status"])
            messages.success(
                request, "Import queued — the importer will process this."
            )
    return redirect("directory_admin:import-detail", public_id=public_id)
