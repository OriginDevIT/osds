"""Full-text search.

- ``search_config_for`` / ``validate_search_config`` -- the tenant's Postgres
  text-search configuration (``tenant.settings["search_config"]``, default
  ``english``, spec issue #31 / ruling 8).
- ``recompute_search_vector`` -- rebuild one listing's ``search_vector``.
  Application-computed (ruling 11): ``upsert_listing`` calls it on every write
  and ``rebuild_search_index`` calls it in bulk.
- ``search`` -- a page of published listings for the public site (used by the
  views in the next PR).

Weights (spec §12.1, §4.5, rulings 5-6): A name, B category names,
C description, D searchable custom fields + locality + region.
"""

from __future__ import annotations

import math

from django.contrib.postgres.search import (
    SearchQuery,
    SearchRank,
    SearchVector,
    TrigramSimilarity,
)
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import ExpressionWrapper, F, FloatField, Q, Value
from django.db.models.expressions import RawSQL
from django.db.models.functions import Coalesce

from directory.models import Listing

DEFAULT_SEARCH_CONFIG = "english"

_SEARCHABLE_CUSTOM_TYPES = {"text", "long_text", "select"}

# Blended score = rank * RANK_SCALE + sim * SIM_SCALE + tier_rank * TIER_BOOST.
# ts_rank / trigram values sit around 0.0-0.3, so the scales keep relevance
# dominant while TIER_BOOST stays a small nudge that mainly breaks near-ties
# (ruling 10).
_RANK_SCALE = 4.0
_SIM_SCALE = 2.0
_TIER_BOOST = 0.05

_HAVERSINE_KM = """
6371 * acos(least(1.0, greatest(-1.0,
    sin(radians(%s)) * sin(radians(lat))
    + cos(radians(%s)) * cos(radians(lat)) * cos(radians(lon) - radians(%s))
)))
"""


def search_config_for(tenant) -> str:
    return (tenant.settings or {}).get("search_config") or DEFAULT_SEARCH_CONFIG


def validate_search_config(name: str) -> None:
    """Raise ``ValueError`` unless ``name`` is a real ``pg_ts_config`` (ruling 8)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_ts_config WHERE cfgname = %s", [name])
        if cursor.fetchone() is None:
            raise ValueError(f"unknown text search configuration: {name!r}")


def _searchable_custom_text(listing) -> str:
    schema = {f["key"]: f for f in (listing.listing_type.fields or [])}
    parts: list[str] = []
    for key, value in (listing.custom_fields or {}).items():
        descriptor = schema.get(key)
        if (
            descriptor
            and descriptor.get("searchable")
            and descriptor.get("type") in _SEARCHABLE_CUSTOM_TYPES
            and value not in (None, "")
        ):
            parts.append(str(value))
    return " ".join(parts)


def reindex_queryset():
    """Base queryset for bulk recompute: the FK and M2M that
    ``recompute_search_vector_v1`` reads, prefetched so a backfill over N
    listings is O(N) UPDATEs, not O(4N) queries. Shared by
    ``rebuild_search_index`` and migration 0004. Iterate with
    ``.iterator(chunk_size=...)`` -- prefetch_related requires it.
    """
    return Listing.objects.select_related("listing_type").prefetch_related(
        "categories"
    )


def recompute_search_vector_v1(listing) -> None:
    """FROZEN migration history -- the signature and behaviour of this function
    must not change.

    Migration ``directory/0004_search_vector`` backfills through this exact
    function. A different weighting, a different input set, or a different
    signature is a NEW function and a NEW migration -- never an edit here.
    ``recompute_search_vector`` delegates to it today.

    Weights: A name, B category names, C description, D searchable custom
    fields + locality + region (spec §12.1, §4.5).
    """
    cfg = search_config_for(listing.tenant)
    category_text = " ".join(c.name for c in listing.categories.all())
    d_text = " ".join(
        part
        for part in (
            _searchable_custom_text(listing),
            listing.locality or "",
            listing.region or "",
        )
        if part
    )
    vector = (
        SearchVector(Value(listing.name), weight="A", config=cfg)
        + SearchVector(Value(category_text), weight="B", config=cfg)
        + SearchVector(Value(listing.description or ""), weight="C", config=cfg)
        + SearchVector(Value(d_text), weight="D", config=cfg)
    )
    Listing.all_tenants.filter(pk=listing.pk).update(search_vector=vector)


def recompute_search_vector(listing) -> None:
    """Rebuild one listing's ``search_vector``. Call with the tenant in scope."""
    recompute_search_vector_v1(listing)


def search(tenant, listing_type, *, q="", near=None, page=1, per_page=20):
    """A page of published listings. ``near`` is ``(lat, lon, radius_km)``.

    Relevance-dominant when there is a query; tier is a bounded boost in the
    blended score. Distance is a secondary tiebreak with a query and the
    primary sort without one (rulings 10-11).
    """
    cfg = search_config_for(tenant)
    q = (q or "").strip()

    qs = Listing.objects.published().filter(listing_type=listing_type)
    qs = qs.annotate(tier_rank=Coalesce("current_tier__rank", Value(0)))

    if near is not None:
        lat0, lon0, radius_km = (float(x) for x in near)
        d_lat = radius_km / 111.045
        d_lon = radius_km / (111.045 * max(0.01, abs(math.cos(math.radians(lat0)))))
        qs = (
            qs.filter(
                lat__range=(lat0 - d_lat, lat0 + d_lat),
                lon__range=(lon0 - d_lon, lon0 + d_lon),
            )
            .annotate(
                distance=RawSQL(
                    _HAVERSINE_KM, [lat0, lat0, lon0], output_field=FloatField()
                )
            )
            .filter(distance__lte=radius_km)
        )

    if q:
        sq = SearchQuery(q, config=cfg, search_type="websearch")
        qs = qs.annotate(
            rank=SearchRank("search_vector", sq),
            sim=TrigramSimilarity("name", q),
        ).filter(Q(search_vector=sq) | Q(name__trigram_similar=q))
        qs = qs.annotate(
            score=ExpressionWrapper(
                Coalesce("rank", Value(0.0)) * Value(_RANK_SCALE)
                + Coalesce("sim", Value(0.0)) * Value(_SIM_SCALE)
                + F("tier_rank") * Value(_TIER_BOOST),
                output_field=FloatField(),
            )
        )
        order = ["-score"]
        if near is not None:
            order.append("distance")
        order += ["name", "public_id"]
    elif near is not None:
        order = ["distance", "-tier_rank", "name", "public_id"]
    else:
        order = ["-tier_rank", "name", "public_id"]

    return Paginator(qs.order_by(*order), per_page).get_page(page)
