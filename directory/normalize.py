"""Input canonicalisation for listing writes.

Applied to every incoming ``listing.upsert`` value so that a byte-identical
re-import produces an empty JSON Patch and no event (spec §7.1). An empty
string collapses to ``None`` -- "cleared" and "never set" are the same state
(ruling 8).
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.utils.text import slugify

_PHONE_STRIP = re.compile(r"[^\d+]")
_LEADING_PLUSES = re.compile(r"^\++")


def text(value):
    """Trim a string; empty -> None. Non-strings pass through."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return value


def email(value):
    value = text(value)
    return value.lower() if value else value


def website(value):
    value = text(value)
    if not value:
        return value
    # lowercase scheme + host, keep the path as given, drop a bare trailing slash
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://[^/]+)(/.*)?$", value)
    if match:
        host = match.group(1).lower()
        rest = match.group(2) or ""
        if rest == "/":
            rest = ""
        return host + rest
    return value


def phone_e164(value):
    value = text(value)
    if not value:
        return value
    cleaned = _PHONE_STRIP.sub("", value)
    cleaned = "+" + _LEADING_PLUSES.sub("", cleaned) if cleaned.startswith("+") else cleaned
    if not re.match(r"^\+\d{8,15}$", cleaned):
        raise ValueError(f"{value!r} is not a valid E.164 phone number")
    return cleaned


def country(value):
    value = text(value)
    return value.upper() if value else value


def slug(value):
    value = text(value)
    return slugify(value) if value else value


def jsonable(value):
    """Recursively coerce ``value`` to JSON-native types for the command log
    and event payloads (spec §7.1, §11.2).

    ``Decimal`` -> ``float`` -- so a coordinate is a JSON *number* from every
    write path, matching ``directory.patch.project`` -- and ``date`` /
    ``datetime`` -> ISO 8601 string. A non-finite float (``nan`` / ``inf``)
    raises ``ValueError``: Postgres ``jsonb`` cannot store it even though
    ``json.dumps`` emits it. So does any value with no JSON representation
    (``set``, ``bytes``, a model instance, ``time``, ``timedelta``). Object
    keys must already be strings. The caller turns ``ValueError`` into a 422.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{value!r} has no JSON representation")
        return value
    if isinstance(value, Decimal):
        num = float(value)
        if not math.isfinite(num):
            raise ValueError(f"{value!r} has no JSON representation")
        return num
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"object key {key!r} is not a string")
            out[key] = jsonable(item)
        return out
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    raise ValueError(f"{type(value).__name__} has no JSON representation")


def decimal6(value):
    """Quantise to the model's 6 dp; -0 -> 0. None/'' -> None."""
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value)).quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{value!r} is not a valid coordinate") from exc
    if d == 0:
        d = Decimal("0.000000")
    return d
