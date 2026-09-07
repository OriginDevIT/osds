"""Attaching and detaching listing media.

Both operations run through here, never through ``listing.upsert`` -- ``media``
is a rejected key on that command (spec §7.1, v0.7). They are siblings of
``directory.services.set_listing_visibility``: a single ``transaction.atomic``
block, the event emitted in the same transaction as the write, and no
command-log row.

The only event either one emits is ``listing.updated``. ``media.*`` is deferred
(spec §3.4): an asset row's ``pending → ready`` move is internal state, and the
fact that matters to an adapter is the ``media`` field changing in the
listing's JSON Patch (decisions.md §4.1).

Uploads are validated, oriented, metadata-stripped and re-encoded in-request
with Pillow. Derivative generation and abuse scanning need the worker, which
does not exist yet -- they are out of scope and no seam is left for them.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from PIL import Image, ImageOps, UnidentifiedImageError

from audit import events
from audit.outbox import emit
from directory.models import Listing, MediaAsset
from directory.patch import diff, project
from directory.storage import get_tenant_storage

# Public URL prefix for the local media-serving view (osds.urls_tenant). A
# cloud backend would instead expose storage.url(key); only local is wired.
MEDIA_URL_PREFIX = "/media/"

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_DIMENSION = 6000

# Pillow format -> (content type, file extension). The closed set of what an
# upload may be.
_ACCEPTED = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}


class MediaError(ValueError):
    """An upload rejected at validation -- bad type, too large, undecodable."""


@dataclass
class _Prepared:
    data: bytes
    content_type: str
    ext: str
    width: int
    height: int
    checksum: str


def _limits() -> tuple[int, int]:
    return (
        getattr(settings, "OSDS_MEDIA_MAX_BYTES", _DEFAULT_MAX_BYTES),
        getattr(settings, "OSDS_MEDIA_MAX_DIMENSION", _DEFAULT_MAX_DIMENSION),
    )


def _prepare_image(upload) -> _Prepared:
    """Validate and normalise an uploaded image.

    Confirms it decodes, is one of the accepted formats, and is within the
    size ceilings; bakes EXIF orientation into the pixels; then rebuilds the
    image from raw pixel data so no metadata (EXIF, GPS, colour profiles,
    comments) survives, and re-encodes to the same format.
    """
    max_bytes, max_dimension = _limits()

    raw = upload.read()
    if not raw:
        raise MediaError("the uploaded file is empty")
    if len(raw) > max_bytes:
        raise MediaError(
            f"the image is {len(raw)} bytes; the limit is {max_bytes}"
        )

    try:
        Image.open(io.BytesIO(raw)).verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise MediaError("the file is not a readable image") from exc

    img = Image.open(io.BytesIO(raw))  # verify() leaves the first image unusable
    fmt = (img.format or "").upper()
    if fmt not in _ACCEPTED:
        raise MediaError(
            f"unsupported image format: {img.format or 'unknown'}. "
            f"Use JPEG, PNG or WebP."
        )
    if max(img.size) > max_dimension:
        raise MediaError(
            f"the image is {img.width}x{img.height}; the limit is "
            f"{max_dimension}px on the longest side"
        )

    try:
        img = ImageOps.exif_transpose(img)  # bake orientation, drop the tag
        mode, size = img.mode, img.size
        pixels = img.tobytes()  # forces a full decode
        palette = img.getpalette() if mode == "P" else None
    except Image.DecompressionBombError as exc:
        raise MediaError("the image is too large to process") from exc

    clean = Image.frombytes(mode, size, pixels)
    if palette is not None:
        clean.putpalette(palette)

    content_type, ext = _ACCEPTED[fmt]
    out = io.BytesIO()
    if fmt == "JPEG":
        clean.convert("RGB").save(out, format="JPEG", quality=90)
    elif fmt == "PNG":
        clean.save(out, format="PNG", optimize=False)
    else:  # WEBP -- has no palette mode
        if clean.mode not in ("RGB", "RGBA"):
            clean = clean.convert("RGBA")
        clean.save(out, format="WEBP", method=4)
    data = out.getvalue()

    return _Prepared(
        data=data,
        content_type=content_type,
        ext=ext,
        width=size[0],
        height=size[1],
        checksum=hashlib.sha256(data).hexdigest(),
    )


def _next_sort_order(listing: Listing, role: str) -> int:
    if role != MediaAsset.Role.GALLERY:
        return 0
    last = (
        MediaAsset.objects.filter(listing=listing, role=role)
        .order_by("-sort_order")
        .first()
    )
    return (last.sort_order + 1) if last is not None else 0


def _ref(asset: MediaAsset) -> dict:
    """The render-ready shape embedded in ``Listing.media``."""
    return {
        "asset_id": asset.public_id,
        "url": f"{MEDIA_URL_PREFIX}{asset.public_id}",
        "width": asset.width,
        "height": asset.height,
        "alt": asset.alt_text or None,
    }


def _rebuild_listing_media(listing: Listing) -> None:
    """Recompute ``Listing.media`` from this listing's ``ready`` assets.

    Rebuilt wholesale every time -- never patched in place -- so it cannot
    drift from the ``MediaAsset`` rows.
    """
    media: dict = {"logo": None, "cover": None, "gallery": []}
    ready = MediaAsset.objects.filter(
        listing=listing, status=MediaAsset.Status.READY
    ).order_by("sort_order", "id")
    for asset in ready:
        if asset.role == MediaAsset.Role.GALLERY:
            media["gallery"].append(_ref(asset))
        else:
            media[asset.role] = _ref(asset)
    listing.media = media


def _emit_listing_updated(listing: Listing, before: dict, *, actor: dict):
    patch = diff(before, project(listing))
    if not patch:
        return None
    return emit(
        events.LISTING_UPDATED,
        subject=listing.public_id,
        tenant=listing.tenant,
        actor=actor,
        data={"changes": patch, "type": listing.listing_type.key},
    )


def attach_media(
    listing: Listing,
    *,
    role: str,
    upload,
    actor: dict,
    alt_text: str = "",
    uploaded_by=None,
) -> MediaAsset:
    """Validate, store and attach one image. Emits ``listing.updated``.

    ``logo`` and ``cover`` are single-valued: attaching a new one discards the
    listing's previous asset of that role. ``gallery`` appends.
    """
    if role not in MediaAsset.Role.values:
        raise MediaError(f"unknown media role: {role!r}")

    tenant = listing.tenant
    prepared = _prepare_image(upload)
    storage = get_tenant_storage(tenant)

    with transaction.atomic():
        before = project(listing)
        asset = MediaAsset.objects.create(
            tenant=tenant,
            listing=listing,
            role=role,
            status=MediaAsset.Status.PENDING,
            original_filename=(getattr(upload, "name", "") or "")[:255],
            content_type=prepared.content_type,
            byte_size=len(prepared.data),
            width=prepared.width,
            height=prepared.height,
            checksum_sha256=prepared.checksum,
            alt_text=(alt_text or "").strip()[:255],
            sort_order=_next_sort_order(listing, role),
            uploaded_by=uploaded_by,
        )
        # The stored object is written inside the transaction. A rollback
        # orphans the file but no row references it; local-disk orphan cleanup
        # is a worker concern once the worker exists.
        key = f"listings/{listing.public_id}/{asset.public_id}.{prepared.ext}"
        # public_id makes the key unique, so save() writes exactly this name;
        # take its return anyway in case a backend normalises it.
        asset.storage_key = storage.save(key, ContentFile(prepared.data))

        if role in (MediaAsset.Role.LOGO, MediaAsset.Role.COVER):
            _discard_role(listing, role, keep=asset, storage=storage)

        asset.status = MediaAsset.Status.READY
        asset.save(update_fields=["storage_key", "status", "updated_at"])

        _rebuild_listing_media(listing)
        listing.save(update_fields=["media", "updated_at"])
        _emit_listing_updated(listing, before, actor=actor)

    return asset


def detach_media(asset: MediaAsset, *, actor: dict) -> None:
    """Remove one asset and its stored object. Emits ``listing.updated``."""
    listing = asset.listing
    storage = get_tenant_storage(asset.tenant)

    with transaction.atomic():
        before = project(listing)
        if asset.storage_key:
            storage.delete(asset.storage_key)
        asset.delete()
        _rebuild_listing_media(listing)
        listing.save(update_fields=["media", "updated_at"])
        _emit_listing_updated(listing, before, actor=actor)


def _discard_role(listing: Listing, role: str, *, keep: MediaAsset, storage) -> None:
    others = MediaAsset.objects.filter(listing=listing, role=role).exclude(
        pk=keep.pk
    )
    for other in others:
        if other.storage_key:
            storage.delete(other.storage_key)
        other.delete()
