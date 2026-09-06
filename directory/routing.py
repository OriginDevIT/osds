"""Public URL construction.

Routing is dynamic -- the ``path_segment`` prefix appears only when the tenant
has more than one listing type (spec §4.5) -- so these build URL strings that
``public_dispatch`` and the templates agree on. Category URLs carry a trailing
slash; listing detail URLs do not.
"""

from __future__ import annotations


def type_url(listing_type, *, multi: bool) -> str:
    return f"/{listing_type.path_segment}/" if multi else "/"


def category_url(listing_type, category, *, multi: bool) -> str:
    prefix = f"/{listing_type.path_segment}" if multi else ""
    return f"{prefix}/{category.slug}/"


def listing_url(listing_type, category, listing, *, multi: bool) -> "str | None":
    """A listing's detail URL under ``category``. ``None`` when there is no
    category -- such a listing has no public detail page (only search)."""
    if category is None:
        return None
    prefix = f"/{listing_type.path_segment}" if multi else ""
    return f"{prefix}/{category.slug}/{listing.slug}"


def canonical_category(listing):
    """The category with the lowest ``(order, slug)`` among the listing's
    categories -- the one its canonical detail URL uses (ruling 3)."""
    categories = list(listing.categories.all())
    if not categories:
        return None
    return min(categories, key=lambda c: (c.order, c.slug))
