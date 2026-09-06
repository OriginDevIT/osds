"""Projecting a Listing into the §4.1 shape, and diffing two projections into
an RFC 6902 JSON Patch for ``listing.updated``.

The projection is the canonical nested structure adapters already see in
``listing.created`` -- not the Django model's attributes -- so patch pointers
line up. What is deliberately absent from the projection never appears in a
patch: ``id``, ``status``, ``visibility``, ``tier``, ``owner``, ``created_at``,
``updated_at``, ``listing_type``, ``tenant``, provenance ids, and the search
vector.
"""

from __future__ import annotations

_MISSING = object()

# Child objects whose own children are leaves. Everything else that is a dict
# (external_profiles, attributes) is compared as one value -- whole-object
# replace (ruling 10).
_RECURSE = {"/location", "/contact", "/media", "/provenance", "/custom_fields"}


def _s(value):
    """Empty string -> None (ruling 8)."""
    if isinstance(value, str):
        return value or None
    return value


def _num(value):
    return float(value) if value is not None else None


def project(listing) -> dict:
    return {
        "slug": listing.slug,
        "name": listing.name,
        "description": _s(listing.description),
        "categories": sorted(c.slug for c in listing.categories.all()),
        "location": {
            "address_line1": _s(listing.address_line1),
            "address_line2": _s(listing.address_line2),
            "locality": _s(listing.locality),
            "region": _s(listing.region),
            "postal_code": _s(listing.postal_code),
            "country": _s(listing.country),
            "lat": _num(listing.lat),
            "lon": _num(listing.lon),
            "geo_precision": listing.geo_precision,
        },
        "contact": {
            "phone_e164": _s(listing.phone_e164),
            "email": _s(listing.email),
            "website": _s(listing.website),
            "social": list(listing.social or []),
        },
        "external_profiles": dict(listing.external_profiles or {}),
        "attributes": dict(listing.attributes or {}),
        "media": {
            "logo": (listing.media or {}).get("logo"),
            "cover": (listing.media or {}).get("cover"),
            "gallery": list((listing.media or {}).get("gallery", [])),
        },
        "reviews_disabled": listing.reviews_disabled,
        "custom_fields": dict(listing.custom_fields or {}),
        "provenance": {
            "source": listing.source,
            "notes": _s(listing.provenance_notes),
        },
    }


def diff(before: dict, after: dict) -> list[dict]:
    return _diff(before, after, "")


def _diff(before: dict, after: dict, path: str) -> list[dict]:
    ops: list[dict] = []
    for key in sorted(set(before) | set(after)):
        pointer = f"{path}/{key}"
        bv = before.get(key, _MISSING)
        av = after.get(key, _MISSING)

        if pointer in _RECURSE and isinstance(bv, dict) and isinstance(av, dict):
            ops.extend(_diff(bv, av, pointer))
            continue

        if _equal(bv, av):
            continue
        if av is _MISSING or av is None:
            ops.append({"op": "remove", "path": pointer})
        elif bv is _MISSING or bv is None:
            ops.append({"op": "add", "path": pointer, "value": av})
        else:
            ops.append({"op": "replace", "path": pointer, "value": av})
    return ops


def _equal(a, b) -> bool:
    a_absent = a is _MISSING or a is None
    b_absent = b is _MISSING or b is None
    if a_absent or b_absent:
        return a_absent and b_absent
    return a == b
