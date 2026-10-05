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

import hmac
import logging
from dataclasses import dataclass
from datetime import timedelta
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
from audit.mail import enqueue
from audit.models import CommandLog, OutboundMessage
from audit.outbox import emit
from audit.ratelimit import RateLimited
from directory import (
    claim_limits,
    claim_review,
    claims,
    normalize,
    routing,
    suppression,
)
from directory.claim_review import expire_pending_code as _expire_pending_code
from directory.claim_review import lock_guard as _lock_guard
from directory.field_schema import (
    SchemaError,
    normalize_type_schema,
    validate_custom_fields,
    validate_type_schema,
)
from directory.masking import mask_email
from directory.models import (
    Category,
    Claim,
    ClaimVerificationGuard,
    Consent,
    ConsentText,
    DirectoryUser,
    ImportBatch,
    ImportBatchListing,
    Listing,
    ListingType,
    ModerationItem,
    PathRedirect,
    SearchReindexJob,
    SuppressionKey,
)
from directory.patch import diff, project
from directory.search import recompute_search_vector
from osds.ids import claim_id
from tenants.claim_verification import CLAIM_VERIFICATION_BOUNDS

logger = logging.getLogger("osds.claims")

# Slugs that would collide with fixed public routes (ruling 20). Rejected in
# create_category and upsert_listing.
RESERVED_SLUGS = frozenset(
    {
        "search",
        "admin",
        "media",
        "claim",
        "lead",
        "owner",
        "_adapters",
        "_internal",
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

        # A lead's consent is evidence (spec §9.0), and Lead.listing and
        # Consent.lead cascade: removing the listing would delete both.
        with_leads = [
            r.listing
            for r in created
            if r.listing is not None and r.listing.leads.exists()
        ]
        if with_leads:
            names = ", ".join(
                sorted(f"{lst.name} ({lst.public_id})" for lst in with_leads)
            )
            raise RollbackRefused(
                "created_listing_has_leads",
                f"these imported listings now carry leads and cannot be "
                f"removed: {names}. A lead's consent record is kept as evidence.",
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


# --- claim.submit (spec §4.3, §9, §9.0, §9.4) -------------------------------
#
# Verification mechanics (code generation, hashing, expiry, attempt limits),
# approval and any write to Listing.status / Listing.owner are a later PR --
# this only ever creates rows at Claim.Status.PENDING_VERIFICATION (or, on the
# dispute branch, DISPUTED) and records intent via `method`.

CONSENT_TEXT_KEY = "consent"

# The three channels the §9 example always carries, one per submission --
# a missing one rejects the whole command (spec §9.0); "not asked" is never
# recorded as "declined".
CONSENT_CHANNELS = ("marketing_email", "marketing_sms", "automated_calls")

DEFAULT_CONSENT_BODY = (
    "PLACEHOLDER -- the operator must replace this with reviewed legal "
    "wording before enabling claims in production.\n\n"
    "By submitting this claim, you confirm you are authorised to manage this "
    "listing on behalf of the business named above. Each consent below is "
    "independent and you may decline any of them without affecting your "
    "claim."
)


class ConsentRequired(Exception):
    """``claim.submit`` is missing one of the required consent channels
    (spec §9.0). Every channel in ``CONSENT_CHANNELS`` must be present --
    an omitted one is rejected outright, never treated as declined."""

    def __init__(self, channel: str):
        self.channel = channel
        super().__init__(f"consent for {channel!r} is required")


def get_default_consent_text(tenant) -> ConsentText:
    """The current claim-consent wording for ``tenant`` (spec §9.0).

    Seeded lazily here, on first read, with ``get_or_create`` -- not in
    ``tenants.services.create_tenant``. Seeding at tenant creation would
    leave every tenant created before this PR shipped unable to take a claim
    until an operator noticed and intervened, and it would make ``tenants``
    import a ``directory`` model, which is backwards: ``directory`` already
    depends on ``tenants``, never the other way (ruling, 2026-09-11).

    No admin UI exists yet to publish a new version, so this is always
    "v1" until one does.
    """
    consent_text, _ = ConsentText.objects.get_or_create(
        tenant=tenant,
        key=CONSENT_TEXT_KEY,
        version="v1",
        defaults={"body": DEFAULT_CONSENT_BODY},
    )
    return consent_text


def enabled_claim_methods(tenant) -> list[str]:
    """The tenant's configured claim-verification methods (spec §9.5), or
    just ``manual`` -- always available, per §9 -- if unset."""
    cfg = (tenant.settings or {}).get("claim_verification") or {}
    return list(cfg.get("enabled_methods") or ["manual"])


def _match_or_create_user(
    tenant, *, email: str, name: str, phone_e164: str
) -> "tuple[DirectoryUser, bool]":
    """Match-or-mint on ``(tenant, email)`` (spec §4.3). A matched row's
    ``name``/``phone_e164`` are left untouched -- this submission's values
    live on the ``claim.submitted`` event regardless, and a claim is not
    licence to overwrite what an earlier claimant recorded."""
    return DirectoryUser.objects.get_or_create(
        tenant=tenant,
        email=email,
        defaults={"name": name, "phone_e164": phone_e164},
    )


def submit_claim(
    tenant,
    *,
    listing: Listing,
    method: str,
    claimant: dict,
    consent: dict,
    ip: "str | None" = None,
) -> Claim:
    """The ``claim.submit`` command.

    ``claimant`` is ``{"name", "email", "phone_e164", "role_claimed"}``.
    ``consent`` is ``{channel: {"granted": bool}}`` for every entry in
    ``CONSENT_CHANNELS``. There is no ``consent_text_version`` parameter --
    the service resolves the current ``ConsentText`` itself
    (``get_default_consent_text``) and records that version. A caller-
    supplied version would let a visitor's own form field decide what
    wording gets attached to their consent record, which is exactly what
    §9.0 exists to prevent: "which version of the wording they saw" has to
    be what the server actually showed, not what the client claims it saw.
    (There is currently only ever one version, "v1", to resolve to -- no
    admin UI exists yet to publish another. When one does, a re-render on a
    server-side mismatch is the fix, not trusting a client-supplied value.)

    Public and visitor-originated, unlike every other orchestrator in this
    module -- there is no operator to log in as, so ``actor`` is
    ``{"type": "visitor", "id": ...}`` throughout. The command-log actor is
    ``claimant:<claim public id>``, minted before the transaction: the user
    row does not exist yet, and the log never carries the claimant's email,
    name or phone (#219). No idempotency key: a
    double form submission is a UI concern (disable-on-submit), not a
    command-log replay, since there is no caller-supplied key to replay on.
    """
    require_autocommit()

    try:
        email = normalize.email(claimant.get("email")) or ""
        if not email:
            raise ValueError("claimant email is required")
        phone = claimant.get("phone_e164") or ""
        if phone:
            try:
                phone = normalize.phone_e164(phone)
            except ValueError:
                # The command log is permanent: the message must not echo
                # the number back (#219).
                raise ValueError("phone is not a valid E.164 number") from None
        name = (claimant.get("name") or "").strip()
        role_claimed = (claimant.get("role_claimed") or "owner").strip() or "owner"
        normalized_claimant = {
            "name": name,
            "email": email,
            "phone_e164": phone,
            "role_claimed": role_claimed,
        }
        # The id is minted here, before the transaction, so the received row
        # can name the claim without naming the claimant (#219). The claimant's
        # details stay on the claim, the user and the event, never in the log.
        claim_public_id = claim_id()
        payload = normalize.jsonable(
            {
                "listing_id": listing.public_id,
                # Validated later, inside the transaction: an unknown value is
                # visitor-supplied text and stays out of the log.
                "method": method if method in Claim.Method.values else None,
                "claim_id": claim_public_id,
                "consent": consent,
            }
        )
    except ValueError as exc:
        # Malformed enough that there is no clean payload to log -- same
        # shape as upsert_listing's equivalent guard (spec §11.2: even a
        # command that never resolves a well-formed body leaves a trace).
        rejected = log_received(
            command="claim.submit",
            tenant=tenant,
            idempotency_key=None,
            actor={"type": "visitor", "id": ""},
            trace_id=None,
            origin="",
            payload=None,
        )
        log_conclude(rejected, outcome="rejected", problem={"payload": str(exc)})
        raise SchemaError([str(exc)]) from exc

    # Rate limit (spec §9.4, #210): after normalisation, because the account
    # key is the normalised email, and before the received row, so a flood
    # writes one ``blocked`` log row per window rather than one per request.
    actor = {"type": "visitor", "id": f"claimant:{claim_public_id}"}
    verdict = claim_limits.check_submit(
        tenant, ip=ip, email=email, now=timezone.now()
    )
    if not verdict.allowed:
        if verdict.first_block:
            blocked = log_received(
                command="claim.submit",
                tenant=tenant,
                idempotency_key=None,
                actor=actor,
                trace_id=None,
                origin="",
                payload=payload,
            )
            log_conclude(
                blocked, outcome="blocked", problem={"rate_limited": verdict.rule}
            )
        raise RateLimited(verdict)

    row = log_received(
        command="claim.submit",
        tenant=tenant,
        idempotency_key=None,
        actor=actor,
        trace_id=None,
        origin="",
        payload=payload,
    )
    try:
        claim, event_id = _apply_submit_claim(
            tenant,
            listing=listing,
            method=method,
            claimant=normalized_claimant,
            consent=consent,
            ip=ip,
            claim_public_id=claim_public_id,
        )
    except ConsentRequired as exc:
        log_conclude(row, outcome="rejected", problem={"missing_consent": exc.channel})
        raise
    except SchemaError as exc:
        log_conclude(row, outcome="rejected", problem={"errors": exc.errors})
        raise
    # Any other exception: the row keeps outcome=NULL (spec §11.2's "threw
    # mid-apply" record). Propagate.

    log_conclude(row, outcome="applied", result_event_id=event_id)
    return claim


@transaction.atomic
def _apply_submit_claim(
    tenant,
    *,
    listing: Listing,
    method: str,
    claimant: dict,
    consent: dict,
    ip: "str | None",
    claim_public_id: str,
) -> "tuple[Claim, str]":
    if method not in Claim.Method.values:
        raise SchemaError(
            [f"method is not one of {', '.join(sorted(Claim.Method.values))}"]
        )
    if method not in enabled_claim_methods(tenant):
        raise SchemaError(
            [f"verification method {method!r} is not enabled for this tenant"]
        )

    missing = [c for c in CONSENT_CHANNELS if c not in consent]
    if missing:
        raise ConsentRequired(missing[0])

    # The version recorded is whatever the server is showing right now, not
    # anything the client claims to have seen (spec §9.0) -- see submit_claim's
    # docstring for why there is no caller-supplied version to resolve instead.
    consent_text = get_default_consent_text(tenant)

    user, minted = _match_or_create_user(
        tenant,
        email=claimant["email"],
        name=claimant["name"],
        phone_e164=claimant["phone_e164"],
    )
    actor = {"type": "visitor", "id": user.public_id}

    # Repeated rejection (spec §9.6) is PR 4's -- nothing writes
    # claim_blocked_at yet -- but submit already refuses a pair it finds
    # blocked (decisions.md §4.4), so the field exists in this migration
    # and is honoured here even though it stays unwritten until PR 4.
    guard = ClaimVerificationGuard.objects.filter(
        tenant=tenant, listing=listing, claimant=user
    ).first()
    if guard is not None and guard.claim_blocked_at is not None:
        raise SchemaError(["this address is blocked from claiming this listing"])

    if minted:
        # Emitted before claim.submitted, same transaction (spec §4.3): the
        # ordering per subject is unambiguous even though both land in one
        # commit.
        emit(
            events.USER_CREATED,
            subject=user.public_id,
            tenant=tenant,
            actor=actor,
            data={
                "user": {
                    "id": user.public_id,
                    "email": user.email,
                    "name": user.name,
                    "phone_e164": user.phone_e164,
                },
                "created_by": "claim.submit",
            },
        )

    # Eligibility is resolved before the row exists (decisions.md §4.4: "an
    # ineligible domain_email claim flips to manual in the service"). A
    # locked (listing, email) pair is ineligible too (claims.
    # domain_email_eligible folds that in) -- so this is the only place PR 3
    # ever produces an ineligible-flip; _apply_start_verification's own
    # ineligible branch is unreachable from here and only fires on a later
    # claim.start_verification (resend/restart), when an import can have
    # changed the website since submit.
    effective_method = method
    review_reason = "chosen" if method == Claim.Method.MANUAL else ""
    if method == Claim.Method.DOMAIN_EMAIL:
        ineligible = claims.domain_email_ineligibility(
            tenant, listing, claimant["email"]
        )
        if ineligible is not None:
            effective_method = Claim.Method.MANUAL
            review_reason = ineligible

    claim = Claim.objects.create(
        tenant=tenant,
        public_id=claim_public_id,
        listing=listing,
        claimant=user,
        method=effective_method,
        requested_method=method,
        review_reason=review_reason,
        role_claimed=claimant["role_claimed"],
        last_step="submitted",
    )

    now = timezone.now()
    consent_payload = {}
    for channel in CONSENT_CHANNELS:
        granted = bool(consent[channel].get("granted"))
        Consent.objects.create(
            tenant=tenant,
            claim=claim,
            channel=channel,
            granted=granted,
            granted_at=now if granted else None,
            ip=ip if granted else None,
            text_version=str(consent_text),
        )
        consent_payload[channel] = {
            "granted": granted,
            "at": now.isoformat() if granted else None,
            "ip": (ip or None) if granted else None,
            "text_version": str(consent_text),
        }

    claim_event = emit(
        events.CLAIM_SUBMITTED,
        subject=claim.public_id,
        tenant=tenant,
        actor=actor,
        data={
            "claim": {
                "id": claim.public_id,
                "listing_id": listing.public_id,
                "status": claim.status,
                "method": claim.method,
            },
            "claimant": {
                "id": user.public_id,
                "name": claimant["name"],
                "email": claimant["email"],
                "phone_e164": claimant["phone_e164"],
                "role_claimed": claimant["role_claimed"],
            },
            "consent": consent_payload,
        },
    )
    result_event_id = claim_event.event_id

    if effective_method == Claim.Method.DOMAIN_EMAIL:
        # Verification starts inside claim.submit (decisions.md §4.4), by
        # calling the same internal function claim.start_verification's
        # wrapper calls -- never duplicated here. A cooldown/lock/cap "not
        # sent" result is silently possible (correction: submit still
        # concludes applied; the claimant sees "no code sent yet" on the
        # verify page) but unreachable in practice for a claim that was
        # just created with an empty guard row.
        claim, _start_result, _start_event_id = _apply_start_verification(
            tenant, claim=claim
        )

    # Two pending claims on a listing that has no sitting owner are not a
    # dispute -- both proceed independently. Approving one is what disposes
    # of the others (auto-rejecting the losers); that reconciliation is a
    # later PR's job, not this check's (ruling, 2026-09-11). Only a listing
    # that is already Status.CLAIMED routes here -- verification alone never
    # moves ownership away from a sitting owner (spec §9.4).
    if listing.status == Listing.Status.CLAIMED:
        claim_review.dispute(tenant, claim=claim, actor=actor)  # notifies
    elif claim.method == Claim.Method.MANUAL:
        # Chosen manual, or flipped to it at submit: in front of a human now.
        claim_review.notify_operators(tenant)

    return claim, result_event_id


# --- domain_email verification (claims PR 3, spec §9.5/§9.6, decisions.md §4.4) ---

COOLDOWN_MINUTES = 15  # 5th wrong entry (spec §9.6)
MAX_WRONG_ENTRIES = 5
MAX_COOLDOWNS = 3  # 3rd cooldown locks the pair (spec §9.6)
MIN_SECONDS_BETWEEN_SENDS = 60
MAX_SENDS_PER_WINDOW = 5
SEND_WINDOW = timedelta(hours=24)


# A disputed claim may still prove its email (decisions.md §4.6): the evidence
# reaches the reviewer, and a correct code never approves one.
_VERIFIABLE_STATUSES = (
    Claim.Status.PENDING_VERIFICATION,
    Claim.Status.DISPUTED,
)


class VerificationRefused(Exception):
    """A guard refusal for claim.start_verification or claim.verify: no
    state change, no event (decisions.md §4.4: "guard refusals emit
    nothing"). Covers the claim-state guards (status/method/verified_at,
    an already-consumed or never-issued code) and the runtime guards
    (cooldown active, pair locked, code expired). ``reason`` is not an
    event field -- it never reaches claim.verification_failed, only the
    command-log ``problem`` and the caller."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _resolve_domain_email_ttl(tenant) -> timedelta:
    """Core computes the lifetime, never the caller (spec §9.5). A stored
    value outside the bounds -- only reachable via direct SQL, since
    tenants.services.update_tenant_settings validates at configuration --
    falls back to the method default with a WARNING; that is a default,
    not a clamp."""
    bounds = CLAIM_VERIFICATION_BOUNDS["domain_email"]
    cfg = (tenant.settings or {}).get("claim_verification") or {}
    minutes = (cfg.get("ttl") or {}).get("domain_email_minutes")
    if minutes is None:
        return timedelta(minutes=bounds["default_minutes"])
    valid = isinstance(minutes, int) and not isinstance(minutes, bool) and (
        bounds["min_minutes"] <= minutes <= bounds["max_minutes"]
    )
    if not valid:
        logger.warning(
            "tenant %s has an invalid domain_email ttl stored (%r minutes); "
            "falling back to the %s-minute default",
            tenant.public_id, minutes, bounds["default_minutes"],
        )
        return timedelta(minutes=bounds["default_minutes"])
    return timedelta(minutes=minutes)


def _flip_to_manual(claim: Claim, *, now, reason: str) -> None:
    """Flip a ``domain_email`` claim that already holds a code to ``manual``
    (decisions.md §4.5: every flip, all reasons): clear ``code_hash`` and
    expire the pending code message in the same transaction, so a code
    queued before the flip is never delivered once mail comes back."""
    claim.method = Claim.Method.MANUAL
    claim.code_hash = ""
    if not claim.review_reason:
        claim.review_reason = reason
    _expire_pending_code(claim, now=now)
    claim.save(update_fields=["method", "code_hash", "review_reason"])


@transaction.atomic
def _apply_start_verification(
    tenant, *, claim: Claim
) -> "tuple[Claim, str, str | None]":
    """The shared start mechanics (decisions.md §4.4: "Both call one
    internal start function"). Called directly -- never through
    ``start_claim_verification``'s command wrapper, which would fail
    ``require_autocommit`` -- by ``_apply_submit_claim`` when the requested
    method is already known eligible, and by ``start_claim_verification``
    for resend/restart.

    ``claim.submit`` resolves eligibility itself before the row even exists
    (spec §9.6's "no website... goes to manual"; decisions.md §4.4), so
    this function's own ineligible branch is unreachable from submit --
    only a later resend/restart reaches it, when an import can have
    changed the website since submit (decisions.md §4.4).

    Returns ``(claim, result, result_event_id)``, ``result_event_id`` the id
    of the event this call itself caused, or ``None`` when it caused none:
      "sent"        -- a new code was generated and queued; result_event_id
                       is its claim.verification_started event.
      "ineligible"  -- flipped to manual, result_event_id is its
                       claim.verification_failed(reason="ineligible") event
                       -- resend/restart only. Also how a locked pair
                       surfaces here: domain_email_eligible folds
                       guard.verification_locked_at into eligibility, so
                       this function never returns a separate "locked"
                       (that reason is still emitted, correctly, from
                       claim.verify's wrong-entry cascade -- the lock's
                       *origin*, not a re-check of it).
      "cooldown" | "cap" -- eligible, but nothing sent: a cooldown is active
        or the send caps are exhausted. No state change, no event -- and
        never raised, so a nested call from claim.submit still lets that
        command conclude "applied". Only ``start_claim_verification``'s
        wrapper turns these into a rejected command-log outcome.

    Raises ``VerificationRefused`` -- state guards only, never reached from
    submit's own call since the claim it just created is always
    pending_verification / domain_email / unverified. The guard row is
    locked, and the claim re-read locked, before any check runs -- same as
    ``_apply_verify_claim_code`` (decisions.md §4.4: no check reads claim
    state before the lock).
    """
    guard = _lock_guard(tenant, listing=claim.listing, claimant=claim.claimant)
    claim = Claim.objects.select_for_update().get(pk=claim.pk)

    if claim.status not in _VERIFIABLE_STATUSES:
        raise VerificationRefused("status")
    if claim.method != Claim.Method.DOMAIN_EMAIL:
        raise VerificationRefused("method")
    if claim.verified_at is not None:
        raise VerificationRefused("verified")

    actor = {"type": "visitor", "id": claim.claimant.public_id}

    ineligible = claims.domain_email_ineligibility(
        tenant, claim.listing, claim.claimant.email
    )
    if ineligible is not None:
        _flip_to_manual(claim, now=timezone.now(), reason=ineligible)
        if claim.status != Claim.Status.DISPUTED:  # a dispute is already queued
            claim_review.notify_operators(tenant)
        event = emit(
            events.CLAIM_VERIFICATION_FAILED,
            subject=claim.public_id,
            tenant=tenant,
            actor=actor,
            data={
                "claim": {
                    "id": claim.public_id,
                    "listing_id": claim.listing.public_id,
                    "status": claim.status,
                    "method": claim.method,
                },
                "method": "domain_email",
                "reason": "ineligible",
                "attempt": None,
                "cooldowns": None,
            },
        )
        return claim, "ineligible", event.event_id

    # A locked pair is already caught above -- domain_email_eligible folds
    # guard.verification_locked_at into eligibility itself (decisions.md
    # §4.4's "a locked pair is ineligible"), so a lock is never seen here.
    now = timezone.now()
    if guard.cooldown_until is not None and guard.cooldown_until > now:
        return claim, "cooldown", None
    if guard.last_code_sent_at is not None and (
        now - guard.last_code_sent_at
    ) < timedelta(seconds=MIN_SECONDS_BETWEEN_SENDS):
        return claim, "cap", None
    window_start = guard.codes_sent_window_start
    sent_in_window = guard.codes_sent_in_window
    if window_start is None or (now - window_start) >= SEND_WINDOW:
        window_start = now
        sent_in_window = 0
    if sent_in_window >= MAX_SENDS_PER_WINDOW:
        return claim, "cap", None

    # A resend supersedes the prior code: the old message, if still
    # pending, dies now rather than being sent after the fact (decisions.md
    # §4.4). Superseding grants no extra guesses -- the counters are on the
    # guard row, untouched here.
    _expire_pending_code(claim, now=now)

    code = claims.generate_code()
    claim.code_hash = claims.hash_code(claim.public_id, code)
    claim.verification_started_at = now
    ttl = _resolve_domain_email_ttl(tenant)
    claim.expires_at = now + ttl

    link_line = ""
    if routing.has_absolute_base(tenant):
        verify_url = routing.absolute_url(tenant, f"/claim/{claim.public_id}/verify/")
        link_line = f"\n\nEnter it at: {verify_url}"
    # No claimant-supplied text (decisions.md §4.4) -- anyone can name
    # another person's address, and this would otherwise let them write
    # into mail sent from the operator's own domain. Body text is exact
    # per decisions.md §4.4's "The message" -- no quotes around names, no
    # isoformat (Y-m-d H:i UTC instead).
    expires_display = claim.expires_at.strftime("%Y-%m-%d %H:%M")
    body_text = (
        f"A request was made on {tenant.name} to claim the listing "
        f"{claim.listing.name} using this email address.\n\n"
        f"Your verification code is: {code}\n\n"
        f"It expires at {expires_display} UTC. Only the most recent code "
        f"works."
        f"{link_line}"
        f"\n\nIf you did not make this request, you can ignore this email. "
        f"Nothing changes unless the code is entered."
    )
    message = enqueue(
        tenant=tenant,
        kind="claim.verification_code",
        to_address=claim.claimant.email,
        subject=f"Your verification code for {claim.listing.name}",
        body_text=body_text,
        expires_at=claim.expires_at,
    )
    claim.code_message = message
    claim.save(
        update_fields=[
            "code_hash", "verification_started_at", "expires_at", "code_message",
        ]
    )

    guard.last_code_sent_at = now
    guard.codes_sent_window_start = window_start
    guard.codes_sent_in_window = sent_in_window + 1
    guard.save(
        update_fields=[
            "last_code_sent_at", "codes_sent_window_start", "codes_sent_in_window",
        ]
    )

    event = emit(
        events.CLAIM_VERIFICATION_STARTED,
        subject=claim.public_id,
        tenant=tenant,
        actor=actor,
        data={
            "claim": {
                "id": claim.public_id,
                "listing_id": claim.listing.public_id,
                "status": claim.status,
                "method": claim.method,
            },
            "method": "domain_email",
            "expires_at": claim.expires_at.isoformat(),
            "destination": mask_email(claim.claimant.email),
        },
    )
    return claim, "sent", event.event_id


def start_claim_verification(
    tenant, *, claim: Claim, ip: "str | None"
) -> Claim:
    """The ``claim.start_verification`` command -- resend/restart. Actor is
    built from ``claim.claimant`` (decisions.md §4.4's "the actor... never
    an email"), never a parameter.

    A cooldown or an exhausted send cap is a guard refusal here --
    concluded ``rejected`` and raised -- even though
    ``_apply_start_verification`` itself never raises for them, so that
    ``claim.submit``'s own nested call (which ignores the returned result)
    still concludes ``applied`` regardless (correction, decisions.md §4.4
    reading). A locked pair takes the "ineligible" path instead -- a real
    state change (flip to manual) -- so it concludes ``applied``, same as
    any other resend that finds the claim ineligible.
    """
    require_autocommit()
    actor = {"type": "visitor", "id": claim.claimant.public_id}
    verdict = claim_limits.check_resend(
        tenant, ip=ip, email=claim.claimant.email, now=timezone.now()
    )
    if not verdict.allowed:
        if verdict.first_block:
            blocked = log_received(
                command="claim.start_verification",
                tenant=tenant,
                idempotency_key=None,
                actor=actor,
                trace_id=None,
                origin="",
                payload={"claim_id": claim.public_id},
            )
            log_conclude(
                blocked, outcome="blocked", problem={"rate_limited": verdict.rule}
            )
        raise RateLimited(verdict)
    row = log_received(
        command="claim.start_verification",
        tenant=tenant,
        idempotency_key=None,
        actor=actor,
        trace_id=None,
        origin="",
        payload={"claim_id": claim.public_id},
    )
    try:
        claim, result, result_event_id = _apply_start_verification(tenant, claim=claim)
    except VerificationRefused as exc:
        log_conclude(row, outcome="rejected", problem={"reason": exc.reason})
        raise
    if result in ("cooldown", "cap"):
        log_conclude(row, outcome="rejected", problem={"reason": result})
        raise VerificationRefused(result)
    log_conclude(row, outcome="applied", result_event_id=result_event_id)
    return claim


@transaction.atomic
def _apply_verify_claim_code(
    tenant, *, claim: Claim, code: str
) -> "tuple[Claim, bool, str | None]":
    """Returns ``(claim, correct, result_event_id)``.

    Locks the guard row, then re-reads and locks the claim row, before any
    check runs (no check reads claim state before the lock). Raises
    ``VerificationRefused`` -- no state change, no event -- for: no active
    code, ``status`` not pending_verification, ``method`` not
    domain_email, already verified, an active cooldown, the lock, or an
    expired code (decisions.md §4.4: "an expired code's entry changes
    nothing").

    Never raises for a wrong code -- it changes state (counters, and
    possibly a cooldown or the lock) and concludes ``applied`` with
    ``result_event_id`` set to its own ``claim.verification_failed``
    (decisions.md §4.4). A correct code concludes ``applied`` with no
    event at all.
    """
    guard = _lock_guard(tenant, listing=claim.listing, claimant=claim.claimant)
    claim = Claim.objects.select_for_update().get(pk=claim.pk)

    # Order matters (decisions.md §4.4, ruling 8): the reason reported is
    # the claim's real state, checked broadest-first -- a claim in the
    # wrong status or method is that, regardless of whether it also happens
    # to have no active code; a locked or cooling-down pair is that, even
    # if the specific code shown would also have expired.
    now = timezone.now()
    if claim.status not in _VERIFIABLE_STATUSES:
        raise VerificationRefused("status")
    if claim.method != Claim.Method.DOMAIN_EMAIL:
        raise VerificationRefused("method")
    if claim.verified_at is not None:
        raise VerificationRefused("verified")
    if guard.verification_locked_at is not None:
        raise VerificationRefused("locked")
    if guard.cooldown_until is not None and guard.cooldown_until > now:
        raise VerificationRefused("cooldown")
    if not claim.code_hash:
        raise VerificationRefused("no_active_code")
    if claim.expires_at is not None and claim.expires_at <= now:
        raise VerificationRefused("expired")

    actor = {"type": "visitor", "id": claim.claimant.public_id}

    if hmac.compare_digest(claim.code_hash, claims.hash_code(claim.public_id, code)):
        claim.verified_at = now
        claim.code_hash = ""
        claim.save(update_fields=["verified_at", "code_hash"])
        if claim.status == Claim.Status.DISPUTED:
            # Evidence for the reviewer only; a code never approves a dispute.
            return claim, True, None
        # Approval happens here, in this transaction (decisions.md §4.4,
        # §4.6): an unowned listing is approved, an owned one disputed.
        event_id = claim_review.resolve_verified_claim(tenant, claim=claim, now=now)
        claim.refresh_from_db()
        return claim, True, event_id

    claim.attempts += 1
    guard.wrong_entries += 1
    attempt_after = guard.wrong_entries  # captured before any reset below
    reason = "wrong_code"
    if guard.wrong_entries >= MAX_WRONG_ENTRIES:
        claim.code_hash = ""
        guard.cooldown_until = now + timedelta(minutes=COOLDOWN_MINUTES)
        guard.cooldowns += 1
        guard.wrong_entries = 0
        reason = "cooldown"
        if guard.cooldowns >= MAX_COOLDOWNS:
            guard.verification_locked_at = now
            claim.method = Claim.Method.MANUAL
            if not claim.review_reason:
                claim.review_reason = "locked"
            _expire_pending_code(claim, now=now)
            reason = "locked"
    cooldowns_after = guard.cooldowns

    claim.save(update_fields=["attempts", "code_hash", "method", "review_reason"])
    guard.save(
        update_fields=[
            "wrong_entries", "cooldown_until", "cooldowns", "verification_locked_at",
        ]
    )

    event = emit(
        events.CLAIM_VERIFICATION_FAILED,
        subject=claim.public_id,
        tenant=tenant,
        actor=actor,
        data={
            "claim": {
                "id": claim.public_id,
                "listing_id": claim.listing.public_id,
                "status": claim.status,
                "method": claim.method,
            },
            "method": "domain_email",
            "reason": reason,
            "attempt": attempt_after,
            "cooldowns": cooldowns_after,
        },
    )
    if reason == "locked":
        # The third cooldown opens the operator's item in this transaction
        # (spec §9.6, #220).
        claim_review.open_item(
            tenant,
            item_type=ModerationItem.ItemType.VERIFICATION_LOCK,
            listing=claim.listing,
            claimant=claim.claimant,
            claim=claim,
            rules=["verification_cooldowns_exhausted"],
            actor=actor,
        )
        if claim.status != Claim.Status.DISPUTED:  # a dispute is already queued
            claim_review.notify_operators(tenant)
    return claim, False, event.event_id


def verify_claim_code(
    tenant, *, claim: Claim, code: str, ip: "str | None"
) -> "tuple[Claim, bool]":
    """The ``claim.verify`` command (spec §9.6, decisions.md §4.4). Payload
    is ``{"claim_id"}`` only -- never the entered code or its hash. Actor is
    built from ``claim.claimant``, never a parameter."""
    require_autocommit()
    actor = {"type": "visitor", "id": claim.claimant.public_id}
    verdict = claim_limits.check_verify(tenant, ip=ip, now=timezone.now())
    if not verdict.allowed:
        if verdict.first_block:
            blocked = log_received(
                command="claim.verify",
                tenant=tenant,
                idempotency_key=None,
                actor=actor,
                trace_id=None,
                origin="",
                payload={"claim_id": claim.public_id},
            )
            log_conclude(
                blocked, outcome="blocked", problem={"rate_limited": verdict.rule}
            )
        raise RateLimited(verdict)
    row = log_received(
        command="claim.verify",
        tenant=tenant,
        idempotency_key=None,
        actor=actor,
        trace_id=None,
        origin="",
        payload={"claim_id": claim.public_id},
    )
    try:
        claim, correct, result_event_id = _apply_verify_claim_code(
            tenant, claim=claim, code=code
        )
    except VerificationRefused as exc:
        log_conclude(row, outcome="rejected", problem={"reason": exc.reason})
        raise
    log_conclude(row, outcome="applied", result_event_id=result_event_id)
    return claim, correct
