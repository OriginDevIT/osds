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

from dataclasses import dataclass

from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from audit import events
from audit.models import CommandLog
from audit.outbox import emit
from directory import normalize
from directory.field_schema import (
    SchemaError,
    normalize_type_schema,
    validate_custom_fields,
    validate_type_schema,
)
from directory.models import (
    Category,
    Listing,
    ListingType,
    PathRedirect,
    SearchReindexJob,
)
from directory.patch import diff, project
from directory.search import recompute_search_vector

# Slugs that would collide with fixed public routes (ruling 20). Rejected in
# create_category and upsert_listing.
RESERVED_SLUGS = frozenset(
    {"search", "admin", "robots.txt", "sitemap.xml", ".well-known"}
)


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
    old_fields = listing_type.fields

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

    # A schema change can flip a field's `searchable` flag, so every listing of
    # this type needs its vector rebuilt (ruling 7).
    if "fields" in changes and changes["fields"] != old_fields:
        SearchReindexJob.objects.create(
            tenant=tenant,
            scope=SearchReindexJob.Scope.LISTING_TYPE,
            scope_ref=listing_type.public_id,
            reason="field schema changed",
        )

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
    if slug in RESERVED_SLUGS:
        raise SchemaError([f"'{slug}' is a reserved slug"])
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


def _category_url_prefix(listing_type: ListingType) -> str:
    """The URL prefix a category sits under: ``/<segment>`` once the tenant is
    multi-type, otherwise empty (spec §4.5, ruling 7 -- one level either way)."""
    multi = (
        ListingType.objects.filter(tenant=listing_type.tenant).count() > 1
    )
    return f"/{listing_type.path_segment}" if multi else ""


@transaction.atomic
def update_category(category: Category, *, actor, **changes) -> Category:
    if changes.get("slug") in RESERVED_SLUGS:
        raise SchemaError([f"'{changes['slug']}' is a reserved slug"])
    tenant = category.tenant
    old_slug = category.slug
    old_name = category.name
    for attr in ("name", "slug", "parent", "order"):
        if attr in changes:
            setattr(category, attr, changes[attr])
    category.save()

    # A slug change moves the category browse page and every listing URL under
    # it. Record the 301, same as a path_segment change (category pages are
    # indexed).
    if "slug" in changes and category.slug != old_slug:
        prefix = _category_url_prefix(category.listing_type)
        PathRedirect.objects.update_or_create(
            tenant=tenant,
            old_prefix=f"{prefix}/{old_slug}",
            defaults={"new_prefix": f"{prefix}/{category.slug}"},
        )

    # The category name feeds weight B of every listing in it (ruling 7).
    if "name" in changes and category.name != old_name:
        SearchReindexJob.objects.create(
            tenant=tenant,
            scope=SearchReindexJob.Scope.CATEGORY,
            scope_ref=category.public_id,
            reason="renamed",
        )

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


# --- listings -------------------------------------------------------------
#
# upsert_listing is the one write path for manual entry, CSV import, owner
# submission and the write API (spec §7.1). It must run in autocommit: the
# command-log rows commit independently of the command transaction (spec
# §11.2). A caller-opened transaction is refused (MustNotBeInTransaction), so
# a batch loops over independent calls rather than wrapping them.


class RejectedField(Exception):
    """A field that listing.upsert refuses outright -> 422 (spec §7.1)."""

    def __init__(self, field: str):
        self.field = field
        super().__init__(f"the field {field!r} is not accepted on listing.upsert")


class MustNotBeInTransaction(RuntimeError):
    """upsert_listing was called inside an open transaction. Its command-log
    rows are committed independently of the command transaction (spec §11.2);
    a caller-opened transaction would pull them in and lose the very
    guarantee the log exists for. Loop over independent calls -- do not wrap
    a batch in one transaction.
    """


@dataclass
class UpsertResult:
    listing: "Listing | None"
    outcome: str  # created | updated | unchanged | replayed
    event_id: "str | None"
    changes: "list | None"


_REJECTED_KEYS = ("tier", "status", "visibility")

_LOCATION_KEYS = (
    "address_line1",
    "address_line2",
    "locality",
    "region",
    "postal_code",
    "country",
)


def _log_received(*, command, tenant, idempotency_key, actor, trace_id, origin, payload):
    return CommandLog.objects.create(
        command=command,
        tenant=tenant,
        idempotency_key=idempotency_key or None,
        adapter_id=origin or "",
        actor=actor or {},
        trace_id=trace_id or "",
        payload=payload,
    )


def _log_conclude(row, *, outcome, result_event_id=None, problem=None):
    row.outcome = outcome
    row.result_event_id = result_event_id or ""
    row.problem = problem
    row.concluded_at = timezone.now()
    row.save(
        update_fields=["outcome", "result_event_id", "problem", "concluded_at"]
    )


def _log_replay(*, command, tenant, idempotency_key, actor, trace_id, prior):
    now = timezone.now()
    CommandLog.objects.create(
        command=command,
        tenant=tenant,
        idempotency_key=idempotency_key or None,
        actor=actor or {},
        trace_id=trace_id or "",
        payload=None,
        outcome="applied",
        result_event_id=prior.result_event_id or "",
        problem={"idempotent_replay": True},
        received_at=now,
        concluded_at=now,
    )


def _resolve_categories(listing_type: ListingType, slugs) -> list[Category]:
    wanted = {s for s in (slugs or []) if s}
    found = list(
        Category.objects.filter(listing_type=listing_type, slug__in=wanted)
    )
    missing = wanted - {c.slug for c in found}
    if missing:
        raise SchemaError([f"unknown category: {s}" for s in sorted(missing)])
    return found


def _apply_geo(listing, loc: dict) -> None:
    if "lat" in loc:
        listing.lat = normalize.decimal6(loc["lat"])
    if "lon" in loc:
        listing.lon = normalize.decimal6(loc["lon"])
    has_coords = listing.lat is not None and listing.lon is not None
    explicit_precision = loc.get("geo_precision")

    if not has_coords:
        listing.lat = None
        listing.lon = None
        listing.geo_precision = Listing.GeoPrecision.NONE
        return

    if explicit_precision == Listing.GeoPrecision.NONE:
        raise SchemaError(
            ["geo_precision cannot be 'none' when coordinates are present"]
        )
    if explicit_precision:
        listing.geo_precision = explicit_precision
    elif listing.geo_precision == Listing.GeoPrecision.NONE:
        listing.geo_precision = Listing.GeoPrecision.LOCALITY  # ruling 6


def _apply_payload(listing, payload, *, listing_type, creating, enforce_required):
    if "name" in payload:
        name = normalize.text(payload["name"])
        if not name:
            raise SchemaError(["name cannot be empty"])
        listing.name = name
    if "slug" in payload:
        raw = str(payload["slug"] or "").strip().lower()
        slug = normalize.slug(payload["slug"])
        if raw in RESERVED_SLUGS or slug in RESERVED_SLUGS:
            raise SchemaError([f"'{payload['slug']}' is a reserved slug"])
        listing.slug = slug
    if "description" in payload:
        listing.description = normalize.text(payload["description"]) or ""
    if "reviews_disabled" in payload:
        listing.reviews_disabled = bool(payload["reviews_disabled"])

    loc = payload.get("location") or {}
    for key in _LOCATION_KEYS:
        if key in loc:
            fn = normalize.country if key == "country" else normalize.text
            setattr(listing, key, fn(loc[key]) or "")
    if any(k in loc for k in ("lat", "lon", "geo_precision")):
        _apply_geo(listing, loc)

    contact = payload.get("contact") or {}
    if "phone_e164" in contact:
        listing.phone_e164 = normalize.phone_e164(contact["phone_e164"]) or ""
    if "email" in contact:
        listing.email = normalize.email(contact["email"]) or ""
    if "website" in contact:
        listing.website = normalize.website(contact["website"]) or ""
    if "social" in contact:
        social = contact["social"]
        if social is None:
            listing.social = []
        elif isinstance(social, list):
            listing.social = social
        else:
            raise SchemaError(["contact.social must be a list"])

    for blob in ("external_profiles", "attributes"):
        if blob in payload:
            value = payload[blob]
            if value is None:
                setattr(listing, blob, {})
            elif isinstance(value, dict):
                setattr(listing, blob, value)
            else:
                raise SchemaError([f"{blob} must be an object"])

    if "media" in payload:
        media = payload["media"]
        listing.media = media if isinstance(media, dict) else {}

    if "custom_fields" in payload:
        cleaned = validate_custom_fields(
            listing_type,
            payload["custom_fields"] or {},
            creating=creating,
            enforce_required=enforce_required,
        )
        current = dict(listing.custom_fields or {})
        for key, value in cleaned.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        listing.custom_fields = current


def _apply_upsert(
    tenant,
    *,
    listing_type,
    payload,
    actor,
    source,
    trace_id,
    origin,
    enforce_required,
    import_batch,
    submitted_by,
    must_create,
) -> UpsertResult:
    with transaction.atomic():
        for key in _REJECTED_KEYS:
            if key in payload:
                raise RejectedField(key)

        listing = None
        if payload.get("id"):
            listing = (
                Listing.objects.select_for_update()
                .filter(tenant=tenant, public_id=payload["id"])
                .first()
            )
            if listing is None:
                raise SchemaError([f"no listing with id {payload['id']}"])
            if listing.listing_type_id != listing_type.id:
                raise RejectedField("listing_type")  # ruling 7
        else:
            match_slug = normalize.slug(payload.get("slug"))
            if match_slug:
                listing = (
                    Listing.objects.select_for_update()
                    .filter(
                        tenant=tenant,
                        listing_type=listing_type,
                        slug=match_slug,
                    )
                    .first()
                )

        creating = listing is None
        if creating:
            if not payload.get("slug") or not payload.get("name"):
                raise SchemaError(["slug and name are required to create a listing"])
            listing = Listing(
                tenant=tenant,
                listing_type=listing_type,
                source=source,
                import_batch=import_batch,
                submitted_by=submitted_by,
            )
            before = None
        else:
            if must_create:
                raise SchemaError(
                    [f"a listing with slug '{listing.slug}' already exists"]
                )
            before = project(listing)

        _apply_payload(
            listing,
            payload,
            listing_type=listing_type,
            creating=creating,
            enforce_required=enforce_required,
        )
        listing.save()

        if "categories" in payload:
            listing.categories.set(
                _resolve_categories(listing_type, payload["categories"])
            )

        # Rebuild the full-text vector on every write (ruling 11).
        recompute_search_vector(listing)

        after = project(listing)

        if creating:
            event = emit(
                events.LISTING_CREATED,
                subject=listing.public_id,
                tenant=tenant,
                data={**after, "type": listing_type.key},
                actor=actor,
                origin=origin,
                trace_id=trace_id or "",
            )
            return UpsertResult(listing, "created", event.event_id, None)

        patch = diff(before, after)
        if not patch:
            return UpsertResult(listing, "unchanged", None, None)

        event = emit(
            events.LISTING_UPDATED,
            subject=listing.public_id,
            tenant=tenant,
            data={"changes": patch, "type": listing_type.key},
            actor=actor,
            origin=origin,
            trace_id=trace_id or "",
        )
        return UpsertResult(listing, "updated", event.event_id, patch)


def upsert_listing(
    tenant,
    *,
    listing_type: ListingType,
    payload: dict,
    actor: dict,
    source: str,
    idempotency_key: "str | None" = None,
    trace_id: "str | None" = None,
    origin: str = "",
    enforce_required: bool = True,
    import_batch=None,
    submitted_by=None,
    must_create: bool = False,
) -> UpsertResult:
    if transaction.get_connection().in_atomic_block:
        raise MustNotBeInTransaction()

    actor = actor or {}

    if idempotency_key:
        prior = (
            CommandLog.objects.filter(
                command="listing.upsert",
                tenant=tenant,
                idempotency_key=idempotency_key,
                outcome="applied",
            )
            .order_by("id")
            .first()
        )
        if prior is not None:
            _log_replay(
                command="listing.upsert",
                tenant=tenant,
                idempotency_key=idempotency_key,
                actor=actor,
                trace_id=trace_id,
                prior=prior,
            )
            return UpsertResult(
                None, "replayed", prior.result_event_id or None, None
            )

    # Written and committed now, independent of the command transaction below:
    # a log row that vanished on rollback would miss the very case it exists for.
    row = _log_received(
        command="listing.upsert",
        tenant=tenant,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        origin=origin,
        payload=payload,
    )

    try:
        result = _apply_upsert(
            tenant,
            listing_type=listing_type,
            payload=payload,
            actor=actor,
            source=source,
            trace_id=trace_id,
            origin=origin,
            enforce_required=enforce_required,
            import_batch=import_batch,
            submitted_by=submitted_by,
            must_create=must_create,
        )
    except RejectedField as exc:
        _log_conclude(row, outcome="rejected", problem={"field": exc.field})
        raise
    except SchemaError as exc:
        _log_conclude(row, outcome="rejected", problem={"errors": exc.errors})
        raise
    # Any other exception: the row keeps outcome=NULL, concluded_at=NULL --
    # that is the "threw mid-apply" record (spec §11.2). Propagate.

    _log_conclude(
        row,
        outcome="applied",
        result_event_id=result.event_id,  # None for an unchanged write (ruling 5)
    )
    return result


@transaction.atomic
def set_listing_visibility(listing: Listing, visibility: str, *, actor, reason="request") -> Listing:
    valid = {
        Listing.Visibility.DRAFT,
        Listing.Visibility.PUBLISHED,
        Listing.Visibility.HIDDEN,
    }
    if visibility not in valid:
        raise ValueError(f"invalid visibility: {visibility!r}")

    old = listing.visibility
    if old == visibility:
        return listing

    listing.visibility = visibility
    listing.save(update_fields=["visibility", "updated_at"])

    published = Listing.Visibility.PUBLISHED
    common = {
        "subject": listing.public_id,
        "tenant": listing.tenant,
        "actor": actor,
    }
    if visibility == published:
        emit(
            events.LISTING_PUBLISHED,
            data={"from": old, "type": listing.listing_type.key, **project(listing)},
            **common,
        )
    elif old == published:
        emit(
            events.LISTING_UNPUBLISHED,
            data={
                "from": old,
                "to": visibility,
                "reason": reason,
                "type": listing.listing_type.key,
            },
            **common,
        )
    # draft <-> hidden crosses no publication boundary -> no event (ruling 2)
    return listing
