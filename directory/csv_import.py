"""CSV import — the target allowlist, upload limits and header read.

PR 1 is upload + mapping only: no worker, no row processing, no ``import.*``
events. The upsert itself, the suppression check and the events land in later
PRs.
"""

from __future__ import annotations

import csv
import io

# An uploaded file larger than this is refused by the upload form.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# More detected header columns than this and the file is refused: a real
# listing CSV has tens of columns, not hundreds, and the cap keeps the
# mapping-form POST under DATA_UPLOAD_MAX_NUMBER_FIELDS.
HEADER_LIMIT = 200

# Bytes read from the file to find the header row. No data rows are parsed in
# the request.
_HEADER_SNIFF_BYTES = 64 * 1024

# Data rows a single worker pass processes before returning. Bounds how long
# one import can hold the worker off the drain.
ROWS_PER_PASS = 50

# A single "categories" column carries several slugs, e.g. "plumbers|emergency".
CATEGORY_SPLIT = "|"

# The upsert targets a CSV column may be mapped to. Every entry is a key path
# that ``directory.services._apply_payload`` / ``_apply_upsert`` actually reads
# -- a drift test enforces it. ``tier``, ``status``, ``visibility`` and
# ``media`` are absent by design: ``listing.upsert`` rejects all four
# (spec §7.1). The only match keys are ``id`` and ``slug``; there is no name or
# address match rule (spec §7.1).
IMPORT_TARGETS: "list[tuple[str, str]]" = [
    ("id", "Match: existing listing ID"),
    ("slug", "Slug (also matches an existing listing)"),
    ("name", "Name"),
    ("description", "Description"),
    ("reviews_disabled", "Reviews disabled"),
    ("categories", "Categories (one column, values separated)"),
    ("location.address_line1", "Address line 1"),
    ("location.address_line2", "Address line 2"),
    ("location.locality", "Locality / city"),
    ("location.region", "Region / state"),
    ("location.postal_code", "Postal code"),
    ("location.country", "Country (ISO 3166-1 alpha-2)"),
    ("location.lat", "Latitude"),
    ("location.lon", "Longitude"),
    ("location.geo_precision", "Geo precision"),
    ("contact.phone_e164", "Phone (E.164)"),
    ("contact.email", "Email"),
    ("contact.website", "Website"),
]


class MappingError(ValueError):
    """A column mapping the operator submitted that cannot be accepted."""


def custom_field_targets(listing_type) -> "list[tuple[str, str]]":
    return [
        (f"custom_fields.{f['key']}", f"Custom: {f['label']}")
        for f in (getattr(listing_type, "fields", None) or [])
    ]


def allowed_targets(listing_type) -> "list[tuple[str, str]]":
    """Every mappable target for this batch: the fixed set plus one per custom
    field on the batch's listing type."""
    return [*IMPORT_TARGETS, *custom_field_targets(listing_type)]


def read_header(raw: bytes) -> "tuple[list[str], str, str]":
    """Return ``(headers, delimiter, encoding)`` from a bounded prefix of the
    uploaded bytes. The header row only -- no data rows are parsed."""
    prefix = raw[:_HEADER_SNIFF_BYTES]
    encoding = "utf-8-sig"
    try:
        text = prefix.decode(encoding)
    except UnicodeDecodeError:
        encoding = "cp1252"
        text = prefix.decode(encoding, errors="replace")

    try:
        delimiter = csv.Sniffer().sniff(text, delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        headers = [h.strip() for h in next(reader)]
    except StopIteration:
        headers = []
    return headers, delimiter, encoding


def validate_mapping(
    submitted: dict, *, headers: "list[str]", listing_type
) -> dict:
    """Clean ``{csv header: target}``: drop ignored columns, reject an unknown
    column, an unknown target, or the same target on two columns. Require a
    match key (``id`` or ``slug``) and -- when not matching by ``id`` -- a
    ``name`` column, since ``slug`` alone cannot create (spec §7.1). Full
    per-row enforcement is the upsert's job in PR 2.
    """
    allowed = {t for t, _ in allowed_targets(listing_type)}
    header_set = set(headers)
    cleaned: "dict[str, str]" = {}
    seen: "set[str]" = set()

    for header, target in submitted.items():
        if not target:
            continue
        if header not in header_set:
            raise MappingError(f"unknown column: {header!r}")
        if target not in allowed:
            raise MappingError(f"{target!r} is not a mappable field")
        if target in seen:
            raise MappingError(f"{target!r} is mapped to more than one column")
        seen.add(target)
        cleaned[header] = target

    if not ({"id", "slug"} & seen):
        raise MappingError("map a column to either the ID or the slug")
    if "id" not in seen and "name" not in seen:
        raise MappingError("map a column to the name (needed to create a listing)")
    return cleaned


def build_payload(cells, headers: "list[str]", column_mapping: dict) -> dict:
    """Turn one CSV data row into a ``listing.upsert`` payload.

    Cells are matched to headers by position. Dotted targets (``location.*``,
    ``contact.*``, ``custom_fields.*``) nest; ``categories`` splits on
    ``CATEGORY_SPLIT``. An unmapped column and an empty cell are both left out
    -- an omitted field is untouched on update (spec §7.1), which is what an
    absent CSV value should mean.
    """
    row = dict(zip(headers, cells))
    payload: dict = {}
    for header, target in column_mapping.items():
        value = (row.get(header) or "").strip()
        if not value:
            continue
        if target == "categories":
            payload["categories"] = [
                part.strip()
                for part in value.split(CATEGORY_SPLIT)
                if part.strip()
            ]
        elif "." in target:
            head, leaf = target.split(".", 1)
            payload.setdefault(head, {})[leaf] = value
        else:
            payload[target] = value
    return payload
