"""The public directory site.

Unauthenticated, read-only, GET only. A single catch-all (``public_dispatch``)
resolves home / type landing / category browse / listing detail against the
tenant's live listing-type count, so the ``path_segment`` prefix is present
only when the tenant has more than one type (spec §4.5).

Only ``Listing.objects.published()`` is ever read here -- a draft, hidden or
unknown slug is a 404 (ruling 13). directory/tests/test_public_site.py guards
that no view uses a bare ``Listing.objects``.
"""

from __future__ import annotations

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Value
from django.db.models.functions import Coalesce
from django.http import FileResponse, Http404, HttpResponsePermanentRedirect
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_safe

from directory import routing
from directory.models import Category, Listing, ListingType, MediaAsset, PathRedirect
from directory.search import search
from directory.storage import get_tenant_storage

_MIN_INDEXABLE = 3  # a category with fewer published listings gets noindex
_RESERVED_TOP = {"robots.txt", "sitemap.xml"}  # PR 4 routes these


# --- helpers ------------------------------------------------------------------


def _page_size() -> int:
    return getattr(settings, "OSDS_PUBLIC_PAGE_SIZE", 20)


def _types(request) -> list[ListingType]:
    return list(ListingType.objects.order_by("id"))


def _type_by_segment(types, segment):
    for lt in types:
        if lt.path_segment == segment:
            return lt
    return None


def _pick_search_type(request, types):
    wanted = request.GET.get("type")
    if wanted:
        for lt in types:
            if wanted in (lt.path_segment, lt.key):
                return lt
    return types[0]


def _parse_near(request):
    raw = request.GET.get("near")
    if not raw:
        return None
    try:
        lat, lon = (float(x) for x in raw.split(",", 1))
        radius = float(request.GET.get("r", "25"))
    except (TypeError, ValueError):
        return None
    if radius <= 0:
        return None
    return (lat, lon, radius)


def _match_redirect(tenant, request_path: str):
    """The single best 301 for ``request_path`` (leading slash included), or
    None. Longest ``old_prefix`` wins; ``""`` prepends ``new_prefix`` to every
    browse path (the tenant gained a second type). One redirect per request --
    no chains (ruling 2)."""
    first = request_path.strip("/").split("/", 1)[0]
    if first in _RESERVED_TOP:
        return None
    rows = sorted(
        PathRedirect.objects.filter(tenant=tenant),
        key=lambda r: len(r.old_prefix),
        reverse=True,
    )
    for row in rows:
        if row.old_prefix == "":
            return row.new_prefix + request_path
        if request_path == row.old_prefix or request_path.startswith(
            row.old_prefix + "/"
        ):
            return row.new_prefix + request_path[len(row.old_prefix):]
    return None


def _category_or_404(listing_type, slug):
    return get_object_or_404(Category, listing_type=listing_type, slug=slug)


# --- views ------------------------------------------------------------------


@require_safe
def home(request):
    types = _types(request)
    if len(types) > 1:
        return render(request, "public/home_types.html", {"types": types})
    listing_type = types[0] if types else None
    categories = (
        Category.objects.filter(listing_type=listing_type, parent__isnull=True)
        if listing_type
        else Category.objects.none()
    )
    return render(
        request,
        "public/home.html",
        {"listing_type": listing_type, "categories": categories, "multi": False},
    )


@require_safe
def search_results(request):
    types = _types(request)
    if not types:
        raise Http404
    multi = len(types) > 1
    listing_type = _pick_search_type(request, types)
    q = request.GET.get("q", "")
    near = _parse_near(request)
    page_obj = search(
        request.tenant,
        listing_type,
        q=q,
        near=near,
        page=request.GET.get("page", 1),
        per_page=_page_size(),
    )
    return render(
        request,
        "public/search.html",
        {
            "listing_type": listing_type,
            "types": types,
            "multi": multi,
            "q": q,
            "near": near,
            "page_obj": page_obj,
        },
    )


@require_safe
def public_dispatch(request, path):
    tenant = request.tenant

    redirect_to = _match_redirect(tenant, "/" + path)
    if redirect_to:
        return HttpResponsePermanentRedirect(redirect_to)

    types = _types(request)
    if not types:
        raise Http404
    multi = len(types) > 1

    segments = [s for s in path.split("/") if s]
    if multi:
        listing_type = _type_by_segment(types, segments[0]) if segments else None
        if listing_type is None:
            raise Http404
        segments = segments[1:]
    else:
        listing_type = types[0]

    if not segments:
        return _type_landing(request, listing_type)  # multi-type only reaches here
    if len(segments) == 1:
        return _category_page(request, listing_type, segments[0], multi=multi)
    if len(segments) == 2:
        return _listing_detail(
            request, listing_type, segments[0], segments[1], multi=multi
        )
    raise Http404


def _type_landing(request, listing_type):
    categories = Category.objects.filter(
        listing_type=listing_type, parent__isnull=True
    )
    return render(
        request,
        "public/type_landing.html",
        {"listing_type": listing_type, "categories": categories, "multi": True},
    )


def _category_page(request, listing_type, slug, *, multi):
    category = _category_or_404(listing_type, slug)
    base = (
        Listing.objects.published()
        .filter(listing_type=listing_type, categories=category)
        .select_related("current_tier")
        .annotate(tier_rank=Coalesce("current_tier__rank", Value(0)))
        .order_by("-tier_rank", "name", "public_id")
    )
    total = base.count()
    page_obj = Paginator(base, _page_size()).get_page(request.GET.get("page", 1))
    noindex = page_obj.number > 1 or total < _MIN_INDEXABLE
    children = Category.objects.filter(listing_type=listing_type, parent=category)
    return render(
        request,
        "public/category.html",
        {
            "listing_type": listing_type,
            "category": category,
            "children": children,
            "page_obj": page_obj,
            "total": total,
            "noindex": noindex,
            "multi": multi,
            "canonical_url": routing.category_url(
                listing_type, category, multi=multi
            ),
        },
    )


def _listing_detail(request, listing_type, category_slug, listing_slug, *, multi):
    listing = get_object_or_404(
        Listing.objects.published().select_related("current_tier", "listing_type"),
        listing_type=listing_type,
        slug=listing_slug,
    )
    category = _category_or_404(listing_type, category_slug)
    if not listing.categories.filter(pk=category.pk).exists():
        raise Http404  # the listing is not in that category

    canonical = routing.canonical_category(listing) or category
    canonical_url = routing.listing_url(
        listing_type, canonical, listing, multi=multi
    )
    public_fields = [
        (f["label"], listing.custom_fields.get(f["key"]))
        for f in (listing_type.fields or [])
        if f.get("public", True) and listing.custom_fields.get(f["key"]) not in (None, "")
    ]
    return render(
        request,
        "public/listing_detail.html",
        {
            "listing": listing,
            "listing_type": listing_type,
            "category": category,
            "categories": listing.categories.all(),
            "canonical_url": canonical_url,
            "public_fields": public_fields,
            "multi": multi,
        },
    )


@require_safe
def media_asset(request, public_id):
    """Stream one ``ready`` media asset from the current tenant's storage.

    ``MediaAsset.objects`` is tenant-scoped, so a guessed id from another
    tenant is a 404 -- the same app-level isolation every other query relies
    on. ``listing__visibility='published'`` keeps a draft or hidden listing's
    images non-public, the same gate the rest of the public site uses (ruling
    13, decisions.md §4.1). Only the local backend serves through this view; a
    cloud backend would hand the browser ``storage.url(key)`` directly.
    """
    asset = get_object_or_404(
        MediaAsset.objects.filter(
            status=MediaAsset.Status.READY,
            listing__visibility=Listing.Visibility.PUBLISHED,
        ),
        public_id=public_id,
    )
    storage = get_tenant_storage(request.tenant)
    try:
        handle = storage.open(asset.storage_key)
    except FileNotFoundError as exc:
        raise Http404 from exc
    response = FileResponse(handle, content_type=asset.content_type or None)
    response["Cache-Control"] = "public, max-age=86400"
    return response


def not_found(request, exception=None):
    return render(request, "public/404.html", status=404)
