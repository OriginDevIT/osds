"""CSV import — the target allowlist, upload limits and header read.

PR 1 is upload + mapping only: no worker, no row processing, no ``import.*``
events. The upsert itself, the suppression check and the events land in later
PRs.
"""

from __future__ import annotations

import csv
import io
import re

from directory.field_schema import CHOICE_TYPES, FIELD_TYPES, validate_type_schema

from directory import normalize
from directory.field_schema import SchemaError

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

# A multi-value column (``categories`` or a ``multi_select`` custom field)
# carries several values in one cell, e.g. "plumbers|emergency". This is the
# separator used unless the operator picks another for that column. It is
# never auto-detected.
DEFAULT_SEPARATOR = "|"

# Longest separator the mapping form accepts.
MAX_SEPARATOR_LENGTH = 5

# Label for the mapping-step option that derives each row's slug from its name.
GENERATE_SLUG_LABEL = (
    "Generate slug from name \u2014 creates new listings only. A row whose "
    "slug already exists is an error, never an update."
)

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


# Match keys are never suggested: a wrong guess there updates the wrong
# listing (spec §7.1). The operator maps these deliberately.
_NEVER_SUGGESTED = frozenset({"id", "slug"})


def suggest_mapping(headers: "list[str]", listing_type) -> "dict[str, str]":
    """Suggest ``{csv header: target}`` for headers that exactly match a
    target's key path (full, or its last segment) or label, ignoring case. Nothing fuzzier.

    Never suggests ``id`` or ``slug``. A header matching more than one target,
    or a target matched by more than one header, gets no suggestion -- a wrong
    guess would write bad data into every row. Suggestions only pre-select the
    form; nothing is saved until the operator submits the mapping.
    """
    names: "dict[str, set[str]]" = {}

    def add(name: str, target: str) -> None:
        names.setdefault(name.strip().casefold(), set()).add(target)

    for target, label in allowed_targets(listing_type):
        if target in _NEVER_SUGGESTED:
            continue
        add(target, target)
        add(target.rsplit(".", 1)[-1], target)  # "locality" for location.locality
        add(label, target)
    for f in getattr(listing_type, "fields", None) or []:
        add(f["label"], f"custom_fields.{f['key']}")

    candidate: "dict[str, str]" = {}
    for header in headers:
        targets = names.get(header.strip().casefold(), set())
        if len(targets) == 1:
            candidate[header] = next(iter(targets))

    counts: "dict[str, int]" = {}
    for target in candidate.values():
        counts[target] = counts.get(target, 0) + 1
    return {h: t for h, t in candidate.items() if counts[t] == 1}


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
    submitted: dict,
    *,
    headers: "list[str]",
    listing_type,
    generate_slug: bool = False,
) -> dict:
    """Clean ``{csv header: target}``: drop ignored columns, reject an unknown
    column, an unknown target, or the same target on two columns. Require a
    match key (``id`` or ``slug``) and -- when not matching by ``id`` -- a
    ``name`` column, since ``slug`` alone cannot create (spec §7.1). Full
    per-row enforcement is the upsert's job in PR 2.

    ``generate_slug`` stands in for the match key: the slug is derived from
    ``name`` per row and the rows only create. It needs a ``name`` column and
    excludes ``slug`` (an explicit slug wins) and ``id`` (a derived slug on an
    id-matched row would rewrite the listing's slug).
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

    if generate_slug:
        if "slug" in seen:
            raise MappingError(
                "a column is mapped to the slug; turn off \"Generate slug from name\""
            )
        if "id" in seen:
            raise MappingError(
                "\"Generate slug from name\" only creates listings and cannot "
                "be combined with a listing ID column"
            )
        if "name" not in seen:
            raise MappingError("map a column to the name to generate slugs from")
        return cleaned
    if not ({"id", "slug"} & seen):
        raise MappingError(
            "map a column to either the ID or the slug, or choose "
            "\"Generate slug from name\""
        )
    if "id" not in seen and "name" not in seen:
        raise MappingError("map a column to the name (needed to create a listing)")
    return cleaned


def multi_value_targets(listing_type) -> "set[str]":
    """Targets whose cell holds several values: ``categories`` and every
    ``multi_select`` custom field on the type."""
    targets = {"categories"}
    for f in getattr(listing_type, "fields", None) or []:
        if f.get("type") == "multi_select":
            targets.add(f"custom_fields.{f['key']}")
    return targets


def clean_separators(submitted: dict, mapping: dict, listing_type=None) -> dict:
    """Keep ``{csv header: separator}`` only for columns mapped to a
    multi-value target and set to something other than the default. A column
    mapped elsewhere, or left blank, is dropped; a separator longer than
    ``MAX_SEPARATOR_LENGTH`` raises ``MappingError``."""
    multi = (
        multi_value_targets(listing_type)
        if listing_type is not None
        else {"categories"} | {t for t in mapping.values() if t.startswith("custom_fields.")}
    )
    cleaned: "dict[str, str]" = {}
    for header, sep in submitted.items():
        if mapping.get(header) not in multi or not sep or sep == DEFAULT_SEPARATOR:
            continue
        if len(sep) > MAX_SEPARATOR_LENGTH:
            raise MappingError(
                f"separator for {header!r} must be at most "
                f"{MAX_SEPARATOR_LENGTH} characters"
            )
        cleaned[header] = sep
    return cleaned


def validate_phone_country(code: str) -> str:
    """Return the upper-cased default country, ``""`` for none, or raise
    ``MappingError`` for a country with no calling-code rule."""
    code = (code or "").strip().upper()
    if code and code not in normalize.PHONE_COUNTRIES:
        raise MappingError(f"{code!r} is not a supported phone country")
    return code


def split_cell(value: str, separator: str = DEFAULT_SEPARATOR) -> "list[str]":
    """Split a multi-value cell into trimmed parts, empty parts dropped."""
    return [p.strip() for p in value.split(separator or DEFAULT_SEPARATOR) if p.strip()]


def category_slug(value: str) -> str:
    """The slug a category cell value resolves to. A value with no usable slug
    is returned as written, so the row fails as an unknown category rather
    than the value being dropped."""
    return normalize.slug(value) or value


def category_values(
    rows,
    headers: "list[str]",
    column_mapping: dict,
    separators: "dict | None" = None,
) -> "list[str]":
    """Distinct ``categories`` cell values across ``rows``, as written, in
    first-seen order, split on the column's separator. Empty when no column is
    mapped to ``categories``."""
    header = next(
        (h for h, t in column_mapping.items() if t == "categories"), None
    )
    if header is None:
        return []
    sep = (separators or {}).get(header, DEFAULT_SEPARATOR)
    seen: "dict[str, None]" = {}
    for cells in rows:
        cell = (dict(zip(headers, cells)).get(header) or "").strip()
        for part in split_cell(cell, sep):
            seen.setdefault(part)
    return list(seen)


def build_payload(
    cells,
    headers: "list[str]",
    column_mapping: dict,
    *,
    listing_type=None,
    phone_country: str = "",
    separators: "dict | None" = None,
) -> dict:
    """Turn one CSV data row into a ``listing.upsert`` payload.

    Cells are matched to headers by position. Dotted targets (``location.*``,
    ``contact.*``, ``custom_fields.*``) nest. ``categories`` and
    ``multi_select`` custom fields split into a list on the column's separator
    (``separators``, default ``DEFAULT_SEPARATOR``). A phone without a leading
    ``+`` is converted with ``phone_country``; with none chosen it is a
    ``SchemaError``, as is an unparseable number. An unmapped column and an
    empty cell are both left out -- an omitted field is untouched on update
    (spec §7.1), which is what an absent CSV value should mean.
    """
    separators = separators or {}
    multi = multi_value_targets(listing_type)
    row = dict(zip(headers, cells))
    payload: dict = {}
    for header, target in column_mapping.items():
        value = (row.get(header) or "").strip()
        if not value:
            continue
        if target in multi:
            parts = split_cell(value, separators.get(header, DEFAULT_SEPARATOR))
            if not parts:
                continue
            if target == "categories":
                payload["categories"] = [category_slug(p) for p in parts]
            else:
                payload.setdefault("custom_fields", {})[target.split(".", 1)[1]] = parts
        elif target == "contact.phone_e164":
            if not value.startswith("+"):
                try:
                    value = normalize.national_phone_to_e164(value, phone_country)
                except ValueError as exc:
                    raise SchemaError([str(exc)]) from exc
            payload.setdefault("contact", {})["phone_e164"] = value
        elif "." in target:
            head, leaf = target.split(".", 1)
            payload.setdefault(head, {})[leaf] = value
        else:
            payload[target] = value
    return payload


# --- inferring a custom-field schema from the file --------------------------
#
# When the upload creates its listing type, every column that maps to no fixed
# target is proposed as one custom field. Nothing about a column's contents is
# sampled: the type is ``text`` and the flags are off until the operator says
# otherwise. ``select`` and ``multi_select`` are not offered -- their option
# list would be a guess; they stay available in the schema builder.
PROPOSAL_TYPES = sorted(FIELD_TYPES - CHOICE_TYPES)


def derive_key(text: str) -> str:
    """Lowercase ``text`` and turn every run of other characters into one
    underscore. The result may still be an invalid key (empty, leading digit);
    that is reported, never repaired."""
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def propose_schema(headers: "list[str]") -> "list[dict]":
    """One proposed ``text`` field per column that suggests no fixed target."""
    mapped = suggest_mapping(headers, None)
    # id and slug are never auto-suggested, but a column named for one is
    # still a column for that fixed target, not a custom field.
    match_names = {
        n.casefold()
        for t, label in IMPORT_TARGETS
        if t in _NEVER_SUGGESTED
        for n in (t, label)
    }
    return [
        {
            "index": i,
            "header": h,
            "key": derive_key(h),
            "label": h,
            "type": "text",
        }
        for i, h in enumerate(headers)
        if h not in mapped and h.casefold() not in match_names
    ]


def _descriptor(row: dict) -> dict:
    return {
        "key": row["key"],
        "label": row["label"],
        "type": row["type"],
        "required": False,
        "public": False,
        "searchable": False,
    }


def check_proposal(rows: "list[dict]") -> "list[list[str]]":
    """Per-row error lists for the proposal, in row order. Uses the same
    schema validator ``create_listing_type`` does, so a row that passes here
    cannot be rejected for its key, label or type at creation."""
    errors: "list[list[str]]" = []
    seen: "set[str]" = set()
    for row in rows:
        found = list(validate_type_schema([_descriptor(row)]))
        if row["type"] not in PROPOSAL_TYPES and row["type"] in FIELD_TYPES:
            found.append("choose a type other than select / multi_select here")
        if row["key"] in seen:
            found.append(f"duplicate key '{row['key']}'")
        seen.add(row["key"])
        errors.append(found)
    return errors


def schema_from_proposal(rows: "list[dict]") -> "list[dict]":
    return [_descriptor(r) for r in rows]
