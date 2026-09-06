"""Service layer for directory configuration.

The tenant-admin views call these; nothing else writes ``ListingType``,
``Category`` or ``PathRedirect``. A listing type is a model row for
queryability, but its creation or change emits ``tenant.settings_changed``
like any other setting (spec §4.5). Patch pointers are a local convention
(ruling 1): ``/listing_types/{key}`` and ``/categories/{type_key}/{slug}``.

Call these with the tenant in ambient scope (a request, or
``osds.tenancy.tenant_context``); the scoped managers depend on it.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import ProtectedError

from audit import events
from audit.outbox import emit
from directory.field_schema import (
    SchemaError,
    normalize_type_schema,
    validate_type_schema,
)
from directory.models import Category, ListingType, PathRedirect


def _actor(operator) -> dict:
    return {"type": "admin", "id": operator.public_id}


def _emit_settings_change(tenant, *, actor, changes: list[dict]) -> None:
    emit(
        events.TENANT_SETTINGS_CHANGED,
        subject=tenant.public_id,
        tenant=tenant,
        actor=_actor(actor),
        data={"changes": changes, "changed_by": actor.public_id},
    )


def _type_value(listing_type: ListingType) -> dict:
    return {
        "key": listing_type.key,
        "label_singular": listing_type.label_singular,
        "label_plural": listing_type.label_plural,
        "path_segment": listing_type.path_segment,
        "claimable": listing_type.claimable,
        "fields": listing_type.fields,
    }


def _category_value(category: Category) -> dict:
    return {
        "name": category.name,
        "parent": category.parent.slug if category.parent_id else None,
        "order": category.order,
    }


# --- listing types -----------------------------------------------------------


@transaction.atomic
def create_listing_type(
    tenant,
    *,
    key: str,
    label_singular: str,
    label_plural: str,
    path_segment: str,
    claimable: bool,
    fields,
    actor,
) -> ListingType:
    errors = validate_type_schema(fields)
    if errors:
        raise SchemaError(errors)

    existing = list(ListingType.objects.filter(tenant=tenant).order_by("id"))
    listing_type = ListingType.objects.create(
        tenant=tenant,
        key=key,
        label_singular=label_singular,
        label_plural=label_plural,
        path_segment=path_segment,
        claimable=claimable,
        fields=normalize_type_schema(fields),
    )

    # First -> second type: the previously implicit no-prefix URLs now live
    # under the first type's segment. Record the 301 (spec §4.5).
    if len(existing) == 1:
        PathRedirect.objects.get_or_create(
            tenant=tenant,
            old_prefix="",
            defaults={"new_prefix": f"/{existing[0].path_segment}"},
        )

    _emit_settings_change(
        tenant,
        actor=actor,
        changes=[
            {
                "op": "add",
                "path": f"/listing_types/{listing_type.key}",
                "value": _type_value(listing_type),
            }
        ],
    )
    return listing_type


@transaction.atomic
def update_listing_type(listing_type: ListingType, *, actor, **changes) -> ListingType:
    if "key" in changes:
        raise ValueError("a listing type's key is frozen after creation")

    tenant = listing_type.tenant
    old_segment = listing_type.path_segment

    if "fields" in changes:
        errors = validate_type_schema(
            changes["fields"], previous=listing_type.fields
        )
        if errors:
            raise SchemaError(errors)
        changes["fields"] = normalize_type_schema(changes["fields"])

    for attr in (
        "label_singular",
        "label_plural",
        "path_segment",
        "claimable",
        "fields",
    ):
        if attr in changes:
            setattr(listing_type, attr, changes[attr])
    listing_type.save()

    # A segment change only affects public URLs once the tenant is multi-type.
    if (
        "path_segment" in changes
        and listing_type.path_segment != old_segment
        and ListingType.objects.filter(tenant=tenant).count() > 1
    ):
        PathRedirect.objects.update_or_create(
            tenant=tenant,
            old_prefix=f"/{old_segment}",
            defaults={"new_prefix": f"/{listing_type.path_segment}"},
        )

    _emit_settings_change(
        tenant,
        actor=actor,
        changes=[
            {
                "op": "replace",
                "path": f"/listing_types/{listing_type.key}",
                "value": _type_value(listing_type),
            }
        ],
    )
    return listing_type


@transaction.atomic
def delete_listing_type(listing_type: ListingType, *, actor) -> None:
    tenant = listing_type.tenant
    key = listing_type.key
    try:
        listing_type.delete()
    except ProtectedError as exc:
        raise ValueError(
            "this type still has listings; move or delete them first"
        ) from exc
    _emit_settings_change(
        tenant,
        actor=actor,
        changes=[{"op": "remove", "path": f"/listing_types/{key}"}],
    )


# --- categories ------------------------------------------------------------


@transaction.atomic
def create_category(
    listing_type: ListingType, *, name: str, slug: str, parent, order: int, actor
) -> Category:
    tenant = listing_type.tenant
    category = Category.objects.create(
        tenant=tenant,
        listing_type=listing_type,
        name=name,
        slug=slug,
        parent=parent,
        order=order,
    )
    _emit_settings_change(
        tenant,
        actor=actor,
        changes=[
            {
                "op": "add",
                "path": f"/categories/{listing_type.key}/{category.slug}",
                "value": _category_value(category),
            }
        ],
    )
    return category


@transaction.atomic
def update_category(category: Category, *, actor, **changes) -> Category:
    tenant = category.tenant
    for attr in ("name", "slug", "parent", "order"):
        if attr in changes:
            setattr(category, attr, changes[attr])
    category.save()
    _emit_settings_change(
        tenant,
        actor=actor,
        changes=[
            {
                "op": "replace",
                "path": f"/categories/{category.listing_type.key}/{category.slug}",
                "value": _category_value(category),
            }
        ],
    )
    return category


@transaction.atomic
def delete_category(category: Category, *, actor) -> None:
    tenant = category.tenant
    type_key = category.listing_type.key
    slug = category.slug
    category.delete()  # children CASCADE; listing associations drop
    _emit_settings_change(
        tenant,
        actor=actor,
        changes=[{"op": "remove", "path": f"/categories/{type_key}/{slug}"}],
    )
