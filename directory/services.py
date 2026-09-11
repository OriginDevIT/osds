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
from decimal import Decimal

from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from audit import events
from audit.command_log import (
    MustNotBeInTransaction,
    log_conclude,
    log_received,
    log_replay,
    require_autocommit,
)
from audit.models import CommandLog
from audit.outbox import emit
from directory import normalize, suppression
from directory.field_schema import (
    SchemaError,
    normalize_type_schema,
    validate_custom_fields,
    validate_type_schema,
)
from directory.models import (
    Category,
    ImportBatch,
    ImportBatchListing,
    Listing,
    ListingType,
    PathRedirect,
    SearchReindexJob,
    SuppressionKey,
)
from directory.patch import diff, project
from directory.search import recompute_search_vector

# Slugs that would collide with fixed public routes (ruling 20). Rejected in
# create_category and upsert_listing.
RESERVED_SLUGS = frozenset(
    {
        "search",
        "admin",
        "media",
        "robots.txt",
        "sitemap.xml",
        "sitemaps",
        ".well-known",
    }
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


class Suppressed(Exception):
    """A create whose (name, address, phone) fingerprint matches a
    ``SuppressionKey`` from a prior deletion (spec §4.1.1). Raised only when
    the caller passes ``suppression_check=True`` -- today just the CSV
    importer, which counts it as ``suppressed`` rather than an error. The
    fingerprint is the one implementation in ``directory.suppression``; no
    matching logic is duplicated here."""

    def __init__(self, key_hash: str):
        self.key_hash = key_hash
        super().__init__("listing suppressed by a prior deletion")


@dataclass
class UpsertResult:
    listing: "Listing | None"
    outcome: str  # created | updated | unchanged | replayed
    event_id: "str | None"
    changes: "list | None"


# media is owned by the media service (directory.media), not writable here:
# spec §7.1 (v0.7) rejects it like tier/status/visibility.
_REJECTED_KEYS = ("tier", "status", "visibility", "media")

_LOCATION_KEYS = (
    "address_line1",
    "address_line2",
    "locality",
    "region",
    "postal_code",
    "country",
)


def _check_suppressed(tenant, payload) -> None:
    """Raise ``Suppressed`` if this create's fingerprint matches a
    ``SuppressionKey``. Called from ``_apply_upsert``'s create branch, after
    the row is known to be a create and before anything is written.

    A blank normalised name is left alone -- ``fingerprint`` would reject it,
    and ``_apply_payload`` rejects the row as an error a moment later."""
    if not normalize.text(payload.get("name")):
        return
    loc = payload.get("location") or {}
    contact = payload.get("contact") or {}
    key_hash = suppression.fingerprint(
        name=payload["name"],
        address_line1=loc.get("address_line1"),
        locality=loc.get("locality"),
        region=loc.get("region"),
        postal_code=loc.get("postal_code"),
        country=loc.get("country"),
        phone=contact.get("phone_e164"),
    )
    if SuppressionKey.objects.filter(tenant=tenant, key_hash=key_hash).exists():
        raise Suppressed(key_hash)


def _record_import_row(import_batch, listing, action: str, pre_image) -> None:
    """Provenance for import rollback (spec §3.3). Called from ``_apply_upsert``
    inside its transaction, so the row commits with the listing change it
    describes. ``pre_image`` is the full projection for an update, ``None`` for
    a create. First touch wins: a ``(batch, listing)`` row already present --
    this batch created the listing and is now updating it in a later CSV row --
    keeps its original ``action`` and ``pre_image``.
    """
    ImportBatchListing.objects.get_or_create(
        batch=import_batch,
        listing=listing,
        defaults={
            "tenant": listing.tenant,
            "action": action,
            "pre_image": pre_image,
        },
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
    # A value that will not normalise is a schema problem, not a crash: the
    # write API and CSV import expect a 422 / row error, and the admin form
    # already renders SchemaError. ``normalize.decimal6`` raises plain
    # ``ValueError``; ``SchemaError`` (a ``ValueError`` subclass) is not raised
    # here, so there is nothing to double-wrap.
    try:
        if "lat" in loc:
            listing.lat = normalize.decimal6(loc["lat"])
        if "lon" in loc:
            listing.lon = normalize.decimal6(loc["lon"])
    except ValueError as exc:
        raise SchemaError([str(exc)]) from exc
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
        # A malformed number is a row-level validation failure -- SchemaError,
        # not an unhandled ValueError that would 500 the admin form or fail a
        # whole CSV batch on one bad cell.
        try:
            listing.phone_e164 = normalize.phone_e164(contact["phone_e164"]) or ""
        except ValueError as exc:
            raise SchemaError([str(exc)]) from exc
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
    suppression_check,
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
            if suppression_check:
                _check_suppressed(tenant, payload)
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
            if import_batch is not None:
                _record_import_row(import_batch, listing, "created", None)
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

        if import_batch is not None:
            _record_import_row(import_batch, listing, "updated", before)

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
    suppression_check: bool = False,
) -> UpsertResult:
    require_autocommit()

    actor = normalize.jsonable(actor or {})

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
            log_replay(
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

    # The payload is stored in the command log and echoed into the event, so it
    # has to be a JSON document before either. Decimal coordinates from the
    # admin form are the common case -- normalize.jsonable turns them into
    # floats, matching directory.patch.project so lat/lon is a JSON number from
    # every write path. A value with no JSON representation (nan/inf, a set,
    # bytes) is a 422, still logged as a rejected attempt (spec §11.2).
    try:
        payload = normalize.jsonable(payload)
    except ValueError as exc:
        rejected = log_received(
            command="listing.upsert",
            tenant=tenant,
            idempotency_key=idempotency_key,
            actor=actor,
            trace_id=trace_id,
            origin=origin,
            payload=None,
        )
        log_conclude(rejected, outcome="rejected", problem={"payload": str(exc)})
        raise SchemaError([str(exc)]) from exc

    # Written and committed now, independent of the command transaction below:
    # a log row that vanished on rollback would miss the very case it exists for.
    row = log_received(
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
            suppression_check=suppression_check,
        )
    except Suppressed as exc:
        log_conclude(row, outcome="rejected", problem={"suppressed": exc.key_hash})
        raise
    except RejectedField as exc:
        log_conclude(row, outcome="rejected", problem={"field": exc.field})
        raise
    except SchemaError as exc:
        log_conclude(row, outcome="rejected", problem={"errors": exc.errors})
        raise
    # Any other exception: the row keeps outcome=NULL, concluded_at=NULL --
    # that is the "threw mid-apply" record (spec §11.2). Propagate.

    log_conclude(
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


# --- import rollback -----------------------------------------------------
#
# rollback_import_batch undoes one CSV import: it deletes the rows the batch
# created and restores the rows it updated from the pre-image captured at
# update time (spec §3.3, decisions.md "Rollback restores updated rows"). It
# emits import.rolled_back and nothing else -- no suppression key, no
# per-listing listing.deleted.


class RollbackRefused(Exception):
    """A rollback precondition failed (spec §3.3). ``reason`` is a
    machine-readable slug for the command-log ``problem``; ``message`` is shown
    to the operator."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        self.message = message
        super().__init__(message)


_ROLLBACKABLE = frozenset(
    {ImportBatch.Status.COMPLETED, ImportBatch.Status.FAILED}
)


def _restore_listing(listing: Listing, pre: dict) -> None:
    """Assign a full §4.1 projection (``directory.patch.project``) back onto a
    listing and rebuild its derived state -- the inverse of ``project``. Used
    only by rollback. It deliberately does not touch ``id``, ``status``,
    ``visibility``, ``tier``, ``owner``, ``listing_type`` or ``media``: the
    projection carries none of those, so a listing claimed, published or
    re-tiered since the import keeps that state.
    """
    loc = pre.get("location") or {}
    contact = pre.get("contact") or {}
    prov = pre.get("provenance") or {}

    listing.slug = pre["slug"]
    listing.name = pre["name"]
    listing.description = pre.get("description") or ""
    listing.reviews_disabled = bool(pre.get("reviews_disabled"))

    listing.address_line1 = loc.get("address_line1") or ""
    listing.address_line2 = loc.get("address_line2") or ""
    listing.locality = loc.get("locality") or ""
    listing.region = loc.get("region") or ""
    listing.postal_code = loc.get("postal_code") or ""
    listing.country = loc.get("country") or ""
    lat, lon = loc.get("lat"), loc.get("lon")
    listing.lat = None if lat is None else Decimal(str(lat))
    listing.lon = None if lon is None else Decimal(str(lon))
    listing.geo_precision = (
        loc.get("geo_precision") or Listing.GeoPrecision.NONE
    )

    listing.phone_e164 = contact.get("phone_e164") or ""
    listing.email = contact.get("email") or ""
    listing.website = contact.get("website") or ""
    listing.social = list(contact.get("social") or [])

    listing.external_profiles = dict(pre.get("external_profiles") or {})
    listing.attributes = dict(pre.get("attributes") or {})
    listing.custom_fields = dict(pre.get("custom_fields") or {})
    listing.source = prov.get("source") or listing.source
    listing.provenance_notes = prov.get("notes") or ""

    listing.save()

    slugs = pre.get("categories") or []
    listing.categories.set(
        Category.objects.filter(
            listing_type_id=listing.listing_type_id, slug__in=slugs
        )
    )

    # Rebuilt, never restored -- the vector is derived and excluded from the
    # projection (ruling 11).
    recompute_search_vector(listing)


def rollback_import_batch(batch: ImportBatch, *, operator) -> str:
    """Undo one import batch and return the ``import.rolled_back`` event id.

    Deletes the rows the batch created, restores the rows it updated from the
    pre-image captured at update time, nulls those pre-images, moves the batch
    to ``rolled_back``, and emits ``import.rolled_back`` -- the only event a
    rollback emits. No ``suppression_key`` is written and no per-listing
    ``listing.deleted`` fires (spec §3.3, decisions.md).

    One transaction for the whole batch. Unlike the import row loop this speaks
    a single command and a single event, so it does not go through
    ``upsert_listing`` per row: the restores and the delete are direct ORM
    writes, and ``import.rolled_back`` is emitted inside the transaction, so
    state and event commit together (spec §11.1).

    Refused (``RollbackRefused``, nothing written): the batch is not
    ``completed`` or ``failed``; any of its pre-images has been nulled -- the
    90-day window has passed (spec §11.2); or a row it *created* now carries a
    ``Claim`` -- deleting a claimed listing is refused and the operator
    resolves the claim first.

    Overlapping batches get no guard. A row updated first by batch A and then
    by batch B holds, in B's pre-image, the state A left it in; rolling B back
    restores whatever the row held when B touched it, and rolling A back
    afterwards restores the pre-A state. ``listings_removed`` /
    ``listings_restored`` count only what this call actually changed, so an
    earlier rollback that removed a shared row simply shrinks a later one's
    counts. Call with the tenant in ambient scope.
    """
    require_autocommit()

    actor = {"type": "admin", "id": operator.public_id}
    log_row = log_received(
        command="import.rollback",
        tenant=batch.tenant,
        idempotency_key=f"import.rollback:{batch.public_id}",
        actor=actor,
        trace_id=None,
        origin="",
        payload={"batch_id": batch.public_id},
    )
    try:
        event_id = _do_rollback(batch, actor=actor, operator=operator)
    except RollbackRefused as exc:
        log_conclude(
            log_row, outcome="rejected", problem={exc.reason: exc.message}
        )
        raise
    log_conclude(log_row, outcome="applied", result_event_id=event_id)
    return event_id


def _do_rollback(batch: ImportBatch, *, actor: dict, operator) -> str:
    if batch.status not in _ROLLBACKABLE:
        raise RollbackRefused(
            "not_rollbackable",
            f"an import in state '{batch.get_status_display()}' cannot be "
            f"rolled back",
        )

    with transaction.atomic():
        locked = ImportBatch.objects.select_for_update().get(pk=batch.pk)
        if locked.status not in _ROLLBACKABLE:
            raise RollbackRefused(
                "not_rollbackable",
                "this import was already rolled back",
            )

        rows = list(
            ImportBatchListing.objects.filter(batch=locked).select_related(
                "listing"
            )
        )
        if any(r.pre_image_nulled_at is not None for r in rows):
            raise RollbackRefused(
                "past_rollback_window",
                "this import is past its 90-day rollback window -- its "
                "pre-images have been cleared",
            )

        created = [
            r for r in rows if r.action == ImportBatchListing.Action.CREATED
        ]
        updated = [
            r for r in rows if r.action == ImportBatchListing.Action.UPDATED
        ]

        claimed = [
            r.listing
            for r in created
            if r.listing is not None and r.listing.claims.exists()
        ]
        if claimed:
            names = ", ".join(
                sorted(f"{lst.name} ({lst.public_id})" for lst in claimed)
            )
            raise RollbackRefused(
                "created_listing_claimed",
                f"these imported listings now carry a claim and cannot be "
                f"removed: {names}. Resolve the claims first.",
            )

        create_ids = [r.listing_id for r in created]
        listings_removed = len(create_ids)
        if create_ids:
            Listing.objects.filter(pk__in=create_ids).delete()

        listings_restored = 0
        for r in updated:
            if r.pre_image is None:
                continue
            listing = Listing.objects.select_for_update().get(pk=r.listing_id)
            _restore_listing(listing, r.pre_image)
            listings_restored += 1

        # The pre-images are a second copy of personal data; a rolled-back
        # batch has no further use for them (spec §11.2, decisions.md).
        ImportBatchListing.objects.filter(batch=locked).update(
            pre_image=None, pre_image_nulled_at=timezone.now()
        )

        locked.status = ImportBatch.Status.ROLLED_BACK
        locked.rolled_back_by = operator
        locked.rolled_back_at = timezone.now()
        locked.save(
            update_fields=["status", "rolled_back_by", "rolled_back_at"]
        )

        event = emit(
            events.IMPORT_ROLLED_BACK,
            subject=locked.public_id,
            tenant=locked.tenant,
            actor=actor,
            data={
                "batch_id": locked.public_id,
                "listings_removed": listings_removed,
                "listings_restored": listings_restored,
                "rolled_back_by": operator.public_id,
            },
        )
    return event.event_id
