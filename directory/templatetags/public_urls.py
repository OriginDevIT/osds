from __future__ import annotations

from django import template

from directory import indexability, routing

register = template.Library()


@register.simple_tag
def category_indexable(published_count, page):
    """The one indexability rule (directory.indexability), so the template and
    the sitemap generator never disagree."""
    return indexability.category_indexable(published_count, page)


@register.simple_tag
def type_url(listing_type, multi):
    return routing.type_url(listing_type, multi=multi)


@register.simple_tag
def category_url(listing_type, category, multi):
    return routing.category_url(listing_type, category, multi=multi)


@register.simple_tag
def listing_url(listing_type, category, listing, multi):
    return routing.listing_url(listing_type, category, listing, multi=multi)


@register.simple_tag
def canonical_category(listing):
    return routing.canonical_category(listing)
