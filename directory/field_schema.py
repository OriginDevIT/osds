"""Validation for a ``ListingType``'s field schema (spec §4.5), and validation
of a listing's ``custom_fields`` against that schema on write.

``validate_type_schema`` is the gate for every schema edit;
``validate_custom_fields`` is the gate for every listing write.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, URLValidator

# Closed set (spec §4.5). A tenant cannot extend it.
FIELD_TYPES = frozenset(
    {
        "text",
        "long_text",
        "integer",
        "decimal",
        "boolean",
        "date",
        "url",
        "email",
        "select",
        "multi_select",
    }
)
CHOICE_TYPES = frozenset({"select", "multi_select"})
SEARCHABLE_TYPES = frozenset({"text", "long_text", "select"})

# The fixed common core (spec §4.5): a custom field may not shadow one.
RESERVED_KEYS = frozenset(
    {
        "id",
        "slug",
        "name",
        "description",
        "status",
        "visibility",
        "tier",
        "categories",
        "location",
        "contact",
        "media",
        "provenance",
        "created_at",
        "updated_at",
        "owner",
    }
)

# The common core as the builder presents it (spec §4.5, fields of §4.1): the
# read-only list shown above the custom rows. ``tokens`` are the words that make
# a custom key a near-duplicate of the core field -- a prefix or suffix on the
# exact name ("business_name") does not escape the reserved-key check's intent.
CORE_FIELDS = (
    {"path": "name", "label": "Name", "tokens": ("name",)},
    {"path": "description", "label": "Description", "tokens": ("description",)},
    {"path": "slug", "label": "Slug", "tokens": ("slug",)},
    {"path": "status", "label": "Status", "tokens": ("status",)},
    {"path": "visibility", "label": "Visibility", "tokens": ("visibility",)},
    {"path": "categories", "label": "Categories", "tokens": ("categories", "category")},
    {"path": "location.address_line1", "label": "Address line 1",
     "tokens": ("address", "address_line1")},
    {"path": "location.address_line2", "label": "Address line 2",
     "tokens": ("address_line2",)},
    {"path": "location.locality", "label": "Locality (city)",
     "tokens": ("locality", "city")},
    {"path": "location.region", "label": "Region (state)",
     "tokens": ("region", "state")},
    {"path": "location.postal_code", "label": "Postal code",
     "tokens": ("postal_code", "postcode", "zip")},
    {"path": "location.country", "label": "Country", "tokens": ("country",)},
    {"path": "location.lat / location.lon", "label": "Coordinates",
     "tokens": ("latitude", "longitude")},
    {"path": "contact.phone", "label": "Phone", "tokens": ("phone",)},
    {"path": "contact.email", "label": "Email", "tokens": ("email",)},
    {"path": "contact.website", "label": "Website", "tokens": ("website",)},
    {"path": "media", "label": "Media (logo, cover, gallery)",
     "tokens": ("logo", "media")},
)


def near_duplicate_warnings(fields) -> list[str]:
    """Warnings for custom keys that shadow a common-core field by another name.

    Advisory, not an error: exact reserved keys are already rejected by
    ``validate_type_schema``; this catches ``business_name`` against ``name``.
    """
    warnings: list[str] = []
    if not isinstance(fields, list):
        return warnings
    for field in fields:
        key = field.get("key") if isinstance(field, dict) else None
        if not isinstance(key, str) or not key or key in RESERVED_KEYS:
            continue
        parts = key.lower().split("_")
        joined = "_".join(parts)
        for core in CORE_FIELDS:
            hit = False
            for token in core["tokens"]:
                n = token.count("_") + 1
                if any("_".join(parts[i : i + n]) == token for i in range(len(parts) - n + 1)):
                    hit = True
                    break
            if hit or joined == core["path"]:
                warnings.append(
                    f"field '{key}' looks like a duplicate of the core field "
                    f"'{core['path']}'. Core fields are what search, the claim "
                    "flow and the public page read; a custom copy is invisible "
                    "to them. Use the core field instead."
                )
                break
    return warnings


_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_KEY_ERROR = "key must be lowercase letters, digits and underscores, starting with a letter"
_BOOL_FLAGS = ("required", "public", "searchable")


def validate_type_key(key: str) -> list[str]:
    """Errors for a listing-type key; the same rule custom field keys follow."""
    if isinstance(key, str) and _KEY_RE.match(key):
        return []
    return [f"type key: {_KEY_ERROR}"]


class SchemaError(ValueError):
    """Raised by ``directory.services`` when a schema edit is rejected.

    ``errors`` is a list of human-readable strings for the form to render.
    """

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def validate_type_schema(fields, *, previous=None) -> list[str]:
    """Return a list of error strings; an empty list means the schema is valid.

    ``previous`` is the type's currently-stored ``fields``. When given, a key
    present in both keeps its ``type`` -- retyping is blocked, add a new key
    (ruling 6). Removing a key is allowed; its stored values are orphaned and
    re-adding the key restores them.
    """
    errors: list[str] = []

    if not isinstance(fields, list):
        return ["schema must be a list of field descriptors"]

    prev_types = {
        f["key"]: f.get("type")
        for f in (previous or [])
        if isinstance(f, dict) and f.get("key")
    }

    seen: set[str] = set()
    for index, field in enumerate(fields):
        where = f"field {index + 1}"
        if not isinstance(field, dict):
            errors.append(f"{where}: not an object")
            continue

        key = field.get("key")
        if not isinstance(key, str) or not key:
            errors.append(f"{where}: key is required")
            key = None
        else:
            where = f"field '{key}'"
            if not _KEY_RE.match(key):
                errors.append(f"{where}: {_KEY_ERROR}")
            if key in RESERVED_KEYS:
                errors.append(f"{where}: '{key}' is a reserved field name")
            if key in seen:
                errors.append(f"{where}: duplicate key")
            seen.add(key)

        label = field.get("label")
        if not isinstance(label, str) or not label.strip():
            errors.append(f"{where}: label is required")

        ftype = field.get("type")
        if ftype not in FIELD_TYPES:
            errors.append(
                f"{where}: type must be one of {', '.join(sorted(FIELD_TYPES))}"
            )
            ftype = None

        for flag in _BOOL_FLAGS:
            if flag in field and not isinstance(field[flag], bool):
                errors.append(f"{where}: {flag} must be true or false")

        if field.get("searchable") and ftype is not None and ftype not in SEARCHABLE_TYPES:
            errors.append(
                f"{where}: only text, long_text and select fields can be searchable"
            )

        options = field.get("options")
        if ftype in CHOICE_TYPES:
            cleaned = [o for o in options if isinstance(o, str) and o.strip()] if isinstance(options, list) else []
            if not isinstance(options, list) or not options:
                errors.append(f"{where}: {ftype} needs a non-empty options list")
            elif len(cleaned) != len(options):
                errors.append(f"{where}: options must be non-empty strings")
            elif len(set(cleaned)) != len(cleaned):
                errors.append(f"{where}: options must be unique")
        elif options not in (None, [], ()):
            errors.append(
                f"{where}: options are only valid on select and multi_select fields"
            )

        if (
            key is not None
            and key in prev_types
            and ftype is not None
            and prev_types[key] is not None
            and ftype != prev_types[key]
        ):
            errors.append(
                f"{where}: type is frozen once the field exists "
                f"(was '{prev_types[key]}'); add a new field with a different key"
            )

    return errors


def normalize_type_schema(fields) -> list[dict]:
    """Canonicalise a *validated* schema for storage: explicit bool flags,
    trimmed labels, and ``options`` only where the type allows it."""
    out: list[dict] = []
    for field in fields:
        normalized = {
            "key": field["key"],
            "label": field["label"].strip(),
            "type": field["type"],
            "required": bool(field.get("required", False)),
            "public": bool(field.get("public", True)),
            "searchable": bool(field.get("searchable", False)),
        }
        if normalized["type"] in CHOICE_TYPES:
            normalized["options"] = [
                o.strip() for o in field.get("options", []) if o and o.strip()
            ]
        out.append(normalized)
    return out


def _coerce(descriptor: dict, raw):
    """Coerce one raw custom-field value to its JSON-storable form, or raise
    ``ValueError`` with a human message."""
    ftype = descriptor["type"]

    if ftype in ("text", "long_text"):
        return str(raw).strip() or None

    if ftype == "integer":
        try:
            return int(raw) if not isinstance(raw, str) else int(raw.strip())
        except (TypeError, ValueError):
            raise ValueError("must be a whole number")

    if ftype == "decimal":
        try:
            return str(Decimal(str(raw).strip()))
        except (InvalidOperation, ValueError):
            raise ValueError("must be a number")

    if ftype == "boolean":
        if raw in (True, "true", "True", "1", 1):
            return True
        if raw in (False, "false", "False", "0", 0):
            return False
        raise ValueError("must be true or false")

    if ftype == "date":
        try:
            return date.fromisoformat(str(raw).strip()).isoformat()
        except ValueError:
            raise ValueError("must be a date (YYYY-MM-DD)")

    if ftype == "url":
        value = str(raw).strip()
        try:
            URLValidator()(value)
        except ValidationError:
            raise ValueError("must be a valid URL")
        return value

    if ftype == "email":
        value = str(raw).strip().lower()
        try:
            EmailValidator()(value)
        except ValidationError:
            raise ValueError("must be a valid email address")
        return value

    if ftype == "select":
        value = str(raw).strip()
        if value not in descriptor.get("options", []):
            raise ValueError("is not one of the allowed options")
        return value

    if ftype == "multi_select":
        if not isinstance(raw, (list, tuple)):
            raise ValueError("must be a list")
        values = [str(v).strip() for v in raw]
        bad = [v for v in values if v not in descriptor.get("options", [])]
        if bad:
            raise ValueError(f"not allowed: {', '.join(bad)}")
        return values

    raise ValueError(f"unknown field type {ftype!r}")


def validate_custom_fields(
    listing_type, values, *, creating: bool, enforce_required: bool = True
) -> dict:
    """Validate and coerce ``values`` (a *partial* custom_fields document)
    against the type schema.

    - An unknown key is rejected (ruling 3).
    - ``required`` is enforced on create, and on any write that touches the
      key, when ``enforce_required`` is set -- never retroactively (spec §4.5).
      Callers pass ``enforce_required=False`` for CSV import only (ruling 4).
    - An explicit ``None`` clears the key; a blank string is treated as ``None``.

    Returns ``{key: coerced_value_or_None}`` for the keys present in ``values``.
    Raises ``SchemaError`` on any problem.
    """
    schema = {f["key"]: f for f in listing_type.fields}
    errors: list[str] = []
    cleaned: dict = {}

    for key in values:
        if key not in schema:
            errors.append(f"'{key}' is not a field on this listing type")

    for key, descriptor in schema.items():
        label = descriptor.get("label", key)
        present = key in values
        raw = values.get(key)
        if isinstance(raw, str) and not raw.strip():
            raw = None

        if present and raw is None:
            cleaned[key] = None
            if descriptor.get("required") and enforce_required:
                errors.append(f"'{label}' is required")
            continue

        if present:
            try:
                cleaned[key] = _coerce(descriptor, raw)
            except ValueError as exc:
                errors.append(f"'{label}' {exc}")
            continue

        if creating and descriptor.get("required") and enforce_required:
            errors.append(f"'{label}' is required")

    if errors:
        raise SchemaError(errors)
    return cleaned
