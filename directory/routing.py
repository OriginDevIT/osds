"""Public URL construction.

Routing is dynamic -- the ``path_segment`` prefix appears only when the tenant
has more than one listing type (spec §4.5) -- so these build URL strings that
``public_dispatch`` and the templates agree on. Category URLs carry a trailing
slash; listing detail URLs do not.

These are also what non-request code uses. ``reverse()`` needs a urlconf on the
thread-local, which only a request sets; the tenant routes live in
``osds.urls_tenant`` (swapped in per request), never the empty root urlconf.
And ``public_dispatch`` is a single catch-all, so ``reverse()`` against it
would only concatenate the path anyway -- see ``docs/decisions.md`` §4.1.
"""

from __future__ import annotations


def has_absolute_base(tenant) -> bool:
    """Whether ``tenant`` has a verified domain, i.e. whether an absolute URL
    can be built for it. Until this is true a public URL only exists as a
    relative path."""
    return bool(tenant.primary_domain) and tenant.domain_verified_at is not None


def absolute_url(tenant, path: str) -> str:
    """Absolute ``https`` URL for a root-relative tenant ``path``, or ``path``
    unchanged until the tenant's domain is verified.

    The scheme is always ``https`` and never branches on ``DEBUG`` or
    ``OSDS_SECURE_COOKIES``. This value is written into durable records read
    off-box -- the ``listing.updated`` outbox payload today, sitemaps later --
    where a scheme that varied with a local dev flag would be wrong
    permanently. A tenant with no verified domain has no absolute form yet, so
    the relative path is returned.
    """
    if has_absolute_base(tenant):
        return f"https://{tenant.primary_domain}{path}"
    return path


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
    categories *of the listing's own type* -- the one its canonical detail URL
    uses (ruling 3).

    A category of another type could otherwise win the ``(order, slug)`` sort
    and produce a URL the site 404s: the detail view resolves the category
    under the listing's type. Constraining here keeps the on-page
    ``rel=canonical`` and the sitemap ``<loc>`` on a path that resolves. The
    write-path hole that lets a cross-type membership exist is #158.
    """
    categories = [
        c for c in listing.categories.all()
        if c.listing_type_id == listing.listing_type_id
    ]
    if not categories:
        return None
    return min(categories, key=lambda c: (c.order, c.slug))
