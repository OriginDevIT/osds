"""What an owner may change on their own listing, and the commands that do it
(spec §15.4 as ruled in decisions.md §4.9).

**Free:** description, phone, the type's *public* custom fields, and media.
**Operator-only:** name, slug, categories, website, email and address. The
contact fields feed defences -- the website's host decides ``domain_email``
eligibility and the listing email is where the anti-hijack notice goes -- and
name, slug and categories change identity, URLs and placement. A key outside
the free set is **refused, not dropped** (spec §7.1: silently ignoring it is
worse, the caller believes the write landed). A refusal is a rejected
``listing.upsert`` row in the command log, written even though no upsert ran.

``owner_update_listing`` is a thin guard over ``upsert_listing``, which stays
the one write path for every population route: the owner is the actor, the
payload is built here from the allowlist and pinned to this listing's id, and
``enforce_required`` stays on ("required is enforced on create and on owner
edit"). Cleared inputs are explicit ``None``, as spec §7.1 asks of owner-facing
edit surfaces.

Ownership is checked live (``Listing.owner``), never read from the session. The
check and the upsert are two transactions, so an owner who loses the listing
in the instant between them can land one last edit; it is an ordinary
``listing.updated`` the new owner can see and revert.

Call with the tenant in ambient scope.
"""

from __future__ import annotations

from audit.command_log import log_conclude, log_received, require_autocommit
from directory import media, services
from directory.models import DirectoryUser, Listing, MediaAsset

# The only keys ``owner_update_listing`` accepts.
FREE_KEYS = frozenset({"description", "phone_e164", "custom_fields"})

# Named so the refusal can say why. Everything else not in FREE_KEYS is
# refused as "not accepted from an owner".
OPERATOR_ONLY_KEYS = frozenset(
    {
        "name", "slug", "categories", "website", "email",
        "address_line1", "address_line2", "locality", "region", "postal_code",
        "country", "lat", "lon", "geo_precision",
        # the payload's own containers, for a caller that builds them
        "location", "contact",
    }
)


class OwnerEditRefused(Exception):
    """A guard refusal: nothing written. ``reason`` is a stable code,
    ``fields`` the offending keys (names only, never values)."""

    def __init__(self, reason: str, fields: "list[str] | None" = None):
        self.reason = reason
        self.fields = sorted(fields or [])
        super().__init__(reason)


REFUSAL_TEXT = {
    "operator_only": (
        "These can only be changed by the directory: {fields}. "
        "Contact the directory to change them."
    ),
    "not_accepted": "These cannot be changed here: {fields}.",
    "custom_field": "These fields cannot be changed here: {fields}.",
    "not_owner": "You do not own this listing.",
    "listing_suspended": "This listing is suspended. Contact the directory.",
}


def refusal_text(exc: OwnerEditRefused) -> str:
    template = REFUSAL_TEXT.get(exc.reason, "This change was refused.")
    return template.format(fields=", ".join(exc.fields))


def _actor(user: DirectoryUser) -> dict:
    return {"type": "owner", "id": user.public_id}


def editable_custom_keys(listing_type) -> "set[str]":
    """The custom fields an owner may change: those the type marks public.
    ``public`` false means admin-only (spec §4.5)."""
    return {f["key"] for f in listing_type.fields if f.get("public", True)}


def _check_ownership(listing: Listing, user: DirectoryUser) -> None:
    if listing.owner_id != user.pk:
        raise OwnerEditRefused("not_owner")
    if listing.status == Listing.Status.SUSPENDED:
        raise OwnerEditRefused("listing_suspended")


def _refuse(tenant, *, user, reason, fields, payload_keys, listing) -> None:
    """Log the refused attempt as a rejected ``listing.upsert`` -- or
    ``blocked`` for someone who is not the owner -- then raise."""
    row = log_received(
        command="listing.upsert", tenant=tenant, idempotency_key=None,
        actor=_actor(user), trace_id=None, origin="",
        # Keys, never values: an owner's rejected input is not worth keeping.
        payload={"id": listing.public_id, "keys": sorted(payload_keys)},
    )
    log_conclude(
        row,
        outcome="blocked" if reason == "not_owner" else "rejected",
        problem={"reason": reason, "fields": sorted(fields)},
    )
    raise OwnerEditRefused(reason, fields)


def owner_update_listing(
    tenant, *, user: DirectoryUser, listing: Listing, changes: dict
):
    """Apply ``changes`` -- ``{"description"?, "phone_e164"?,
    "custom_fields"?: {key: value}}`` -- to ``listing`` as ``owner``.

    A value of ``None`` (or a blank string) clears the field. Returns the
    ``UpsertResult``: ``updated`` with a ``listing.updated`` event, or
    ``unchanged`` and nothing emitted. Raises ``OwnerEditRefused`` for a key
    outside the owner's scope, a listing that is not theirs or is suspended,
    and ``SchemaError`` for a value the schema rejects.
    """
    require_autocommit()
    keys = set(changes)

    operator_only = keys & OPERATOR_ONLY_KEYS
    if operator_only:
        _refuse(tenant, user=user, reason="operator_only", fields=operator_only,
                payload_keys=keys, listing=listing)
    unknown = keys - FREE_KEYS
    if unknown:
        _refuse(tenant, user=user, reason="not_accepted", fields=unknown,
                payload_keys=keys, listing=listing)

    custom = changes.get("custom_fields") or {}
    not_editable = set(custom) - editable_custom_keys(listing.listing_type)
    if not_editable:
        # An unknown key or an admin-only one: either way not the owner's.
        _refuse(tenant, user=user, reason="custom_field", fields=not_editable,
                payload_keys=keys, listing=listing)

    # Live ownership, re-read -- the caller's object may be stale.
    fresh = Listing.objects.filter(pk=listing.pk).first()
    if fresh is None or fresh.owner_id != user.pk:
        _refuse(tenant, user=user, reason="not_owner", fields=set(),
                payload_keys=keys, listing=listing)
    if fresh.status == Listing.Status.SUSPENDED:
        _refuse(tenant, user=user, reason="listing_suspended", fields=set(),
                payload_keys=keys, listing=listing)

    payload: dict = {"id": listing.public_id}
    if "description" in changes:
        payload["description"] = _blank_to_none(changes["description"])
    if "phone_e164" in changes:
        payload["contact"] = {"phone_e164": _blank_to_none(changes["phone_e164"])}
    if "custom_fields" in changes:
        payload["custom_fields"] = {k: _blank_to_none(v) for k, v in custom.items()}

    return services.upsert_listing(
        tenant,
        listing_type=listing.listing_type,
        payload=payload,
        actor=_actor(user),
        source="owner_submission",
        enforce_required=True,
    )


def _blank_to_none(value):
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


# --- media --------------------------------------------------------------------


def owner_attach_media(
    tenant, *, user: DirectoryUser, listing: Listing, role: str, upload, alt_text: str = ""
) -> MediaAsset:
    """Attach one image through the media service, as the owner. The service
    validates and strips the bytes, stores them and emits ``listing.updated``.
    ``MediaError`` and ``DeferredFeatureError`` propagate."""
    _guard(listing, user)
    return media.attach_media(
        listing, role=role, upload=upload, actor=_actor(user), alt_text=alt_text,
        uploaded_by=None,  # MediaAsset.uploaded_by points at an operator
    )


def owner_detach_media(
    tenant, *, user: DirectoryUser, listing: Listing, asset: MediaAsset
) -> None:
    _guard(listing, user)
    if asset.listing_id != listing.pk:
        raise OwnerEditRefused("not_owner")
    media.detach_media(asset, actor=_actor(user))


def _guard(listing: Listing, user: DirectoryUser) -> None:
    fresh = Listing.objects.filter(pk=listing.pk).first()
    if fresh is None:
        raise OwnerEditRefused("not_owner")
    _check_ownership(fresh, user)
