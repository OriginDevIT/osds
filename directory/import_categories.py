"""The "create missing categories" step of a CSV import.

The mapped ``categories`` column names categories by value. This module reads
the file, compares the distinct values with the batch listing type's own
categories, and -- only when the operator confirms -- creates the missing ones
through ``services.create_category``.

Nothing here infers anything beyond the slug: a created category is top level,
named exactly as the value was written, with the default order. Lookup and
creation are both scoped to the batch's listing type, so a category of the same
slug under another type is neither matched nor reused.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import IntegrityError

from directory import csv_import, normalize, services
from directory.field_schema import SchemaError
from directory.importing import _read_rows
from directory.models import Category

_SLUG_MAX = Category._meta.get_field("slug").max_length
_NAME_MAX = Category._meta.get_field("name").max_length


@dataclass
class CategoryPlan:
    """``existing`` and ``missing`` are ``(value, slug)``; ``flagged`` is
    ``(value, reason)``. A flagged value is never created."""

    existing: "list[tuple[str, str]]" = field(default_factory=list)
    missing: "list[tuple[str, str]]" = field(default_factory=list)
    flagged: "list[tuple[str, str]]" = field(default_factory=list)


def plan_for_batch(batch) -> CategoryPlan:
    """Read the batch's file and plan its categories. Raises
    ``ImportFileError`` when the file cannot be read."""
    rows = _read_rows(batch)
    values = csv_import.category_values(
        rows, batch.detected_headers or [],
        batch.column_mapping or {},
        batch.column_separators or {},
    )
    return plan(batch.listing_type, values)


def plan(listing_type, values: "list[str]") -> CategoryPlan:
    slugs = {v: csv_import.category_slug(v) for v in values}
    have = set(
        Category.objects.filter(
            listing_type=listing_type, slug__in=set(slugs.values())
        ).values_list("slug", flat=True)
    )
    out = CategoryPlan()
    by_slug: "dict[str, list[str]]" = {}
    for value in values:
        slug = slugs[value]
        if slug in have:
            out.existing.append((value, slug))
        else:
            by_slug.setdefault(slug, []).append(value)

    for value in values:
        slug = slugs[value]
        if slug in have:
            continue
        group = by_slug[slug]
        reason = None
        if not normalize.slug(value):
            reason = "no usable slug can be derived from this value"
        elif slug.lower() in services.RESERVED_SLUGS or value.lower() in services.RESERVED_SLUGS:
            reason = f"'{slug}' is a reserved slug"
        elif len(slug) > _SLUG_MAX or len(value) > _NAME_MAX:
            reason = "value is too long for a category name or slug"
        elif len(group) > 1:
            others = ", ".join(repr(v) for v in group if v != value)
            reason = f"derives the same slug '{slug}' as {others}"
        if reason:
            out.flagged.append((value, reason))
        else:
            out.missing.append((value, slug))
    return out


def create_missing(listing_type, category_plan: CategoryPlan, *, actor):
    """Create every ``missing`` entry under ``listing_type``. Returns
    ``(created slugs, [(slug, reason)] failed)``. One failure does not stop the
    rest."""
    created: "list[str]" = []
    failed: "list[tuple[str, str]]" = []
    for value, slug in category_plan.missing:
        try:
            services.create_category(
                listing_type, name=value, slug=slug, parent=None, order=0, actor=actor
            )
        except SchemaError as exc:
            failed.append((slug, "; ".join(map(str, exc.errors))))
        except IntegrityError:
            failed.append((slug, "a category with this slug already exists"))
        else:
            created.append(slug)
    return created, failed
