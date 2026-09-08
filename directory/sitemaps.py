"""robots.txt and the sitemap index for a tenant's public site.

The generator runs outside a request: every function takes ``tenant`` and
builds URLs with ``directory.routing.absolute_url`` -- never ``reverse()``,
never ``request.urlconf`` (decisions.md §4.1, #122). It is called live from
the public views today; worker precompute is #159.

Structure (spec §12.2):

* ``/sitemap.xml`` is always a ``<sitemapindex>``, even with a single child.
* Children are ``/sitemaps/listings-N.xml`` and ``/sitemaps/categories-N.xml``,
  sharded at ``SITEMAP_SHARD_SIZE`` URLs.
* Listings child: the home page, then every published listing at its canonical
  category path, ``<lastmod>`` from ``Listing.updated_at``.
* Categories child: category page 1 for every category with at least
  ``MIN_INDEXABLE_LISTINGS`` published listings. No ``<lastmod>``.
* No ``changefreq``, no ``priority``.

A tenant with no verified domain has no absolute URLs, so ``build_index`` /
``build_child`` are not called for it (the views 404) and ``build_robots``
returns ``Disallow: /`` with no ``Sitemap`` line.
"""

from __future__ import annotations

import math
from xml.sax.saxutils import escape

from django.db.models import Count, F, Q

from directory import indexability, routing
from directory.models import Category, Listing, ListingType
from osds.tenancy import tenant_context

# Sitemap protocol caps a file at 50,000 URLs (spec §12.2).
SITEMAP_SHARD_SIZE = 50_000

_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

# (loc, lastmod-or-None)
_Entry = tuple[str, "str | None"]


# --- robots.txt ------------------------------------------------------------


def build_robots(tenant) -> str:
    """robots.txt body. Until the domain is verified there is no absolute
    ``Sitemap:`` target, so the whole site is disallowed."""
    if not routing.has_absolute_base(tenant):
        return "User-agent: *\nDisallow: /\n"
    return (
        "User-agent: *\n"
        "Disallow: /admin\n"
        "Disallow: /search\n"
        f"Sitemap: {routing.absolute_url(tenant, '/sitemap.xml')}\n"
    )


# --- sitemap index and children -----------------------------------------------


def build_index(tenant) -> str:
    """The ``<sitemapindex>``. Always at least one child (``listings-1.xml``,
    which carries the home page)."""
    with tenant_context(tenant):
        listing_shards = _shard_count(_listing_entries(tenant))
        category_shards = _shard_count(_category_entries(tenant))

    children = [f"/sitemaps/listings-{n}.xml" for n in range(1, listing_shards + 1)]
    children += [f"/sitemaps/categories-{n}.xml" for n in range(1, category_shards + 1)]
    locs = [routing.absolute_url(tenant, path) for path in children]
    return _render_sitemapindex(locs)


def build_child(tenant, kind: str, shard: int) -> "str | None":
    """One ``<urlset>``. ``None`` -> the view returns 404 (unknown kind, shard
    out of range, or an empty categories family)."""
    if kind not in ("listings", "categories") or shard < 1:
        return None

    with tenant_context(tenant):
        entries = (
            _listing_entries(tenant)
            if kind == "listings"
            else _category_entries(tenant)
        )

    count = _shard_count(entries)
    if shard > count:
        return None
    lo = (shard - 1) * SITEMAP_SHARD_SIZE
    return _render_urlset(entries[lo:lo + SITEMAP_SHARD_SIZE])


# --- URL sets (call within tenant_context) -----------------------------------


def _multi(tenant) -> bool:
    return ListingType.objects.count() > 1


def _listing_entries(tenant) -> list[_Entry]:
    multi = _multi(tenant)
    entries: list[_Entry] = [(routing.absolute_url(tenant, "/"), None)]
    listings = (
        Listing.objects.published()
        .select_related("listing_type")
        .prefetch_related("categories")
        .order_by("public_id")
    )
    for listing in listings.iterator(chunk_size=500):
        category = routing.canonical_category(listing)
        if category is None:
            continue  # no category -> no detail page, only search
        path = routing.listing_url(
            listing.listing_type, category, listing, multi=multi
        )
        entries.append(
            (routing.absolute_url(tenant, path), listing.updated_at.isoformat())
        )
    return entries


def _category_entries(tenant) -> list[_Entry]:
    multi = _multi(tenant)
    rows = (
        Category.objects.select_related("listing_type")
        .annotate(
            published=Count(
                "listings",
                filter=Q(
                    listings__visibility=Listing.Visibility.PUBLISHED,
                    listings__listing_type=F("listing_type"),
                ),
                distinct=True,
            )
        )
        .order_by("order", "slug", "public_id")
    )
    return [
        (
            routing.absolute_url(
                tenant, routing.category_url(row.listing_type, row, multi=multi)
            ),
            None,
        )
        for row in rows
        if indexability.category_indexable(row.published)
    ]


# --- rendering -------------------------------------------------------------


def _shard_count(entries: list) -> int:
    return math.ceil(len(entries) / SITEMAP_SHARD_SIZE) if entries else 0


def _render_sitemapindex(locs: list[str]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<sitemapindex xmlns="{_NS}">',
    ]
    lines += [f"  <sitemap><loc>{escape(loc)}</loc></sitemap>" for loc in locs]
    lines.append("</sitemapindex>")
    return "\n".join(lines) + "\n"


def _render_urlset(entries: list[_Entry]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<urlset xmlns="{_NS}">',
    ]
    for loc, lastmod in entries:
        if lastmod:
            lines.append(
                f"  <url><loc>{escape(loc)}</loc>"
                f"<lastmod>{escape(lastmod)}</lastmod></url>"
            )
        else:
            lines.append(f"  <url><loc>{escape(loc)}</loc></url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"
