"""robots.txt and the sitemap index for a tenant's public site.

The generator runs outside a request: every function takes ``tenant`` and
builds URLs with ``directory.routing.absolute_url`` -- never ``reverse()``,
never ``request.urlconf`` (decisions.md §4.1, #122). It is called live from
the public views as the fallback, and by the worker's ``sitemaps`` tick job
(#159), which renders every file once and stores it in the tenant's storage.
The views serve the stored files and generate live only for a tenant that has
none (cloud storage, #150, or a fresh install before the first tick).

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

import hashlib
import json
import math
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

from django.core.files.base import ContentFile
from django.db.models import Count, F, Max, Q, Sum

from directory import indexability, routing
from directory.models import Category, Listing, ListingType
from directory.storage import get_tenant_storage
from osds.tenancy import tenant_context

# Sitemap protocol caps a file at 50,000 URLs (spec §12.2).
SITEMAP_SHARD_SIZE = 50_000

_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

# A rebuilt sitemap is not rebuilt again inside this window, however much the
# listings churn: a bulk import changes the fingerprint on every row, and a
# 50,000-URL render per row is the load this job exists to remove.
REBUILD_MIN_INTERVAL = timedelta(minutes=5)

_DIR = "sitemaps"
_POINTER = f"{_DIR}/current.json"

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


# --- stored artifact (#159) ---------------------------------------------------
#
# Layout in the tenant's storage:
#
#   sitemaps/current.json          pointer: generation, fingerprint, base, time
#   sitemaps/<generation>/index.xml, listings-N.xml, categories-N.xml
#
# A generation directory is written in full and only then pointed at, so a
# reader sees the old sitemap or the new one, never a half-written one.
# ``FileSystemStorage.save`` never overwrites (it renames on collision), so the
# pointer is delete-then-save; in the instant between the two the pointer is
# absent and the view falls back to live generation.


def fingerprint(tenant) -> str:
    """A digest of everything the sitemap is built from, cheap enough to compute
    every tick: a handful of aggregates, no iteration over listings.

    * published listings: count, ``max(updated_at)``, ``sum(id)`` -- a publish
      and an unpublish in one interval change the id sum even if the count
      holds;
    * listing-to-category membership of published listings, as a count and a
      ``sum(listing_id * category_id)`` -- moving a listing between categories
      changes its canonical path;
    * every category's and listing type's routing fields (few, hashed whole);
    * the tenant's absolute base, because every ``<loc>`` embeds it.
    """
    with tenant_context(tenant):
        listings = Listing.objects.published().aggregate(
            n=Count("id"), newest=Max("updated_at"), ids=Sum("id")
        )
        membership = Listing.categories.through.objects.filter(
            listing__tenant=tenant,
            listing__visibility=Listing.Visibility.PUBLISHED,
        ).aggregate(n=Count("id"), pairs=Sum(F("listing_id") * F("category_id")))
        categories = list(
            Category.objects.order_by("id").values_list(
                "id", "slug", "order", "listing_type_id", "parent_id"
            )
        )
        types = list(
            ListingType.objects.order_by("id").values_list(
                "id", "path_segment", "updated_at"
            )
        )
    payload = json.dumps(
        [
            [str(v) for v in listings.values()],
            [str(v) for v in membership.values()],
            [[str(v) for v in row] for row in categories],
            [[str(v) for v in row] for row in types],
            routing.absolute_url(tenant, "/"),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def render_all(tenant) -> "dict[str, str]":
    """Every sitemap file for ``tenant``, keyed by file name, each URL set built
    once. (``build_index`` plus ``build_child`` per shard rebuilt the whole
    listing set on every request.)"""
    with tenant_context(tenant):
        listings = _listing_entries(tenant)
        categories = _category_entries(tenant)

    files: "dict[str, str]" = {}
    names: list[str] = []
    for kind, entries in (("listings", listings), ("categories", categories)):
        for shard in range(1, _shard_count(entries) + 1):
            lo = (shard - 1) * SITEMAP_SHARD_SIZE
            name = f"{kind}-{shard}.xml"
            files[name] = _render_urlset(entries[lo:lo + SITEMAP_SHARD_SIZE])
            names.append(name)
    locs = [routing.absolute_url(tenant, f"/{_DIR}/{name}") for name in names]
    files["index.xml"] = _render_sitemapindex(locs)
    return files


class StoredSitemaps:
    """The current generation of a tenant's stored sitemap."""

    def __init__(self, storage, pointer: dict):
        self._storage = storage
        self.pointer = pointer

    def get(self, name: str) -> "str | None":
        path = f"{_DIR}/{self.pointer['generation']}/{name}"
        try:
            with self._storage.open(path, "rb") as fh:
                return fh.read().decode("utf-8")
        except OSError:
            return None


def _read_pointer(storage) -> "dict | None":
    try:
        with storage.open(_POINTER, "rb") as fh:
            pointer = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(pointer, dict) or "generation" not in pointer:
        return None
    return pointer


def load(tenant) -> "StoredSitemaps | None":
    """The stored sitemap for ``tenant``, or ``None``: no generation yet, the
    storage backend unavailable, or a generation built for a different absolute
    base than the tenant has now (its ``<loc>`` values would be wrong)."""
    try:
        storage = get_tenant_storage(tenant)
    except Exception:  # DeferredFeatureError today; never fail a request on it
        return None
    pointer = _read_pointer(storage)
    if pointer is None or pointer.get("base") != routing.absolute_url(tenant, "/"):
        return None
    return StoredSitemaps(storage, pointer)


def _remove_generation(storage, generation: str) -> None:
    directory = f"{_DIR}/{generation}"
    try:
        _, files = storage.listdir(directory)
    except OSError:
        return
    for name in files:
        storage.delete(f"{directory}/{name}")
    storage.delete(directory)


def _remove_all(storage) -> bool:
    try:
        directories, files = storage.listdir(_DIR)
    except OSError:
        return False
    for generation in directories:
        _remove_generation(storage, generation)
    for name in files:
        storage.delete(f"{_DIR}/{name}")
    storage.delete(_DIR)
    return True


def publish(tenant, *, now, digest: str) -> None:
    """Render and store a new generation, point at it, drop the old one."""
    storage = get_tenant_storage(tenant)
    previous = _read_pointer(storage)
    generation = f"g{int(now.timestamp() * 1000)}"

    for name, body in render_all(tenant).items():
        storage.save(f"{_DIR}/{generation}/{name}", ContentFile(body.encode("utf-8")))

    pointer = {
        "generation": generation,
        "fingerprint": digest,
        "base": routing.absolute_url(tenant, "/"),
        "generated_at": now.isoformat(),
    }
    if storage.exists(_POINTER):
        storage.delete(_POINTER)
    storage.save(_POINTER, ContentFile(json.dumps(pointer).encode("utf-8")))

    if previous and previous["generation"] != generation:
        _remove_generation(storage, previous["generation"])


def _plan(tenant, storage, *, now) -> "tuple[str, str | None]":
    """What ``refresh`` would do, without doing it: ``"clear"``, ``"current"``,
    ``"debounced"`` or ``"rebuild"`` (with the fingerprint to store)."""
    if not routing.has_absolute_base(tenant):
        # An unverified domain serves no sitemap (decisions.md §4.1); any
        # generation left from before the domain changed must go.
        return ("clear" if storage.exists(_DIR) else "current"), None
    digest = fingerprint(tenant)
    pointer = _read_pointer(storage)
    if pointer is not None:
        if pointer.get("fingerprint") == digest:
            return "current", digest
        if now - datetime.fromisoformat(pointer["generated_at"]) < REBUILD_MIN_INTERVAL:
            return "debounced", digest
    return "rebuild", digest


def needs_rebuild(tenant, *, now) -> bool:
    """Whether ``refresh`` would render ``tenant`` right now. Read-only."""
    try:
        storage = get_tenant_storage(tenant)
    except Exception:
        return False
    return _plan(tenant, storage, now=now)[0] == "rebuild"


def refresh(tenant, *, now) -> str:
    """Bring ``tenant``'s stored sitemap up to date. Returns what happened:
    ``"unavailable"`` (storage backend not implemented), ``"cleared"`` (no
    verified domain, so no sitemap exists), ``"current"``, ``"debounced"`` or
    ``"rebuilt"``. Idempotent: an unchanged fingerprint is ``"current"``."""
    try:
        storage = get_tenant_storage(tenant)
    except Exception:
        return "unavailable"
    plan, digest = _plan(tenant, storage, now=now)
    if plan == "clear":
        _remove_all(storage)
        return "cleared"
    if plan == "rebuild":
        publish(tenant, now=now, digest=digest)
        return "rebuilt"
    return plan


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
