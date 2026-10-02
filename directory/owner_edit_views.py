"""The owner's listing page, edit and media routes, and the leads placeholder
(decisions.md §4.9).

``/owner/listings/<id>/`` is the read view with the edit form. Each POST route
calls exactly one command in ``directory.owner_edit``. Ownership is resolved
live on every request: a listing that is not the owner's is a 404.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from directory import media as media_service
from directory import owner_edit, routing
from directory.field_schema import SchemaError
from directory.models import Listing, MediaAsset
from directory.owner_auth import owner_required
from directory.owner_forms import (
    OwnerMediaForm,
    build_owner_edit_form_class,
    changes_from_form,
    initial_from_listing,
)
from directory.storage import DeferredFeatureError

# Keys the write path refuses outright (spec §7.1) plus identity, which a
# crafted form post might try. Anything else unknown is just noise.
_SMUGGLED = frozenset({"tier", "status", "visibility", "media", "id", "owner", "owner_id"})


def _owned(request):
    return (
        Listing.objects.filter(owner=request.owner)
        .select_related("listing_type", "current_tier")
        .order_by("name", "id")
    )


def _own_listing(request, public_id):
    return get_object_or_404(_owned(request), public_id=public_id)


def _stray_keys(post, form) -> list[str]:
    """Names in the POST that are not the form's own and that the owner-edit
    service refuses: an operator-only key, or one the write path rejects.
    Django would silently ignore them; the spec's rule is refuse."""
    own = set(form.fields) | {"csrfmiddlewaretoken"}
    return [
        k for k in post
        if k not in own and (k in owner_edit.OPERATOR_ONLY_KEYS or k in _SMUGGLED)
    ]


def _manage_context(request, listing, form):
    category = routing.canonical_category(listing)
    multi = request.tenant.listing_types.count() > 1
    public_path = routing.listing_url(listing.listing_type, category, listing, multi=multi)
    return {
        "listing": listing,
        "form": form,
        "media_form": OwnerMediaForm(),
        "assets": MediaAsset.objects.filter(listing=listing).order_by("role", "sort_order"),
        "public_path": public_path if listing.visibility == "published" else None,
        "editable": listing.status != Listing.Status.SUSPENDED,
    }


@never_cache
@owner_required
def listing_manage(request, public_id):
    listing = _own_listing(request, public_id)
    form_class = build_owner_edit_form_class(listing.listing_type)
    editable = listing.status != Listing.Status.SUSPENDED

    if request.method == "POST":
        if not editable:
            messages.error(request, owner_edit.REFUSAL_TEXT["listing_suspended"])
            return redirect("owner-listing", public_id=public_id)
        form = form_class(request.POST)
        stray = _stray_keys(request.POST, form)
        if form.is_valid() or stray:
            changes = changes_from_form(form) if form.is_valid() else {}
            changes.update({k: request.POST.get(k) for k in stray})
            try:
                result = owner_edit.owner_update_listing(
                    request.tenant, user=request.owner, listing=listing, changes=changes
                )
            except owner_edit.OwnerEditRefused as exc:
                messages.error(request, owner_edit.refusal_text(exc))
            except SchemaError as exc:
                for message in exc.errors:
                    form.add_error(None, message)
            else:
                messages.success(
                    request, "No changes." if result.outcome == "unchanged" else "Saved."
                )
                return redirect("owner-listing", public_id=public_id)
    else:
        form = form_class(initial=initial_from_listing(listing, form_class))

    return render(request, "owner/listing_manage.html", _manage_context(request, listing, form))


@owner_required
@require_POST
def media_add(request, public_id):
    listing = _own_listing(request, public_id)
    form = OwnerMediaForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose an image and a placement.")
    else:
        try:
            owner_edit.owner_attach_media(
                request.tenant, user=request.owner, listing=listing,
                role=form.cleaned_data["role"], upload=form.cleaned_data["image"],
                alt_text=form.cleaned_data["alt_text"],
            )
        except owner_edit.OwnerEditRefused as exc:
            messages.error(request, owner_edit.refusal_text(exc))
        except (media_service.MediaError, DeferredFeatureError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Image added.")
    return redirect("owner-listing", public_id=public_id)


@owner_required
@require_POST
def media_remove(request, public_id, asset_public_id):
    listing = _own_listing(request, public_id)
    asset = get_object_or_404(
        MediaAsset.objects.filter(listing=listing), public_id=asset_public_id
    )
    try:
        owner_edit.owner_detach_media(
            request.tenant, user=request.owner, listing=listing, asset=asset
        )
    except owner_edit.OwnerEditRefused as exc:
        messages.error(request, owner_edit.refusal_text(exc))
    except DeferredFeatureError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Image removed.")
    return redirect("owner-listing", public_id=public_id)


@owner_required
def leads(request):
    """A placeholder: the owner's leads page arrives with lead capture, which
    has no producer yet. 404 until then, and the dashboard does not link it."""
    raise Http404("lead capture has not shipped")
