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


def require_text(value, field: str):
    """``value`` if it is ``None`` or a string, else ``ValueError`` naming
    ``field``. The message never carries the value: it reaches the permanent
    command log (#219)."""
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    return value


def require_mapping(value, field: str) -> dict:
    """``value`` if it is a dict, else ``ValueError`` naming ``field`` (no
    value echoed, as ``require_text``)."""
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


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
        raise ValueError("phone is not a valid E.164 number")
    return cleaned


# ISO 3166-1 alpha-2 -> (calling code, national numbers carry a leading "0"
# trunk prefix that is dropped in E.164). Only countries whose rule is
# unambiguous are listed; a country not listed cannot be chosen as a default.
# "NANP" countries share +1 and are handled by their own rule below.
PHONE_COUNTRIES: "dict[str, tuple[str, bool]]" = {
    "US": ("1", False), "CA": ("1", False),
    "GB": ("44", True), "IE": ("353", True), "AU": ("61", True),
    "NZ": ("64", True), "DE": ("49", True), "FR": ("33", True),
    "NL": ("31", True), "BE": ("32", True), "CH": ("41", True),
    "AT": ("43", True), "SE": ("46", True), "FI": ("358", True),
    "ES": ("34", False), "PT": ("351", False), "PL": ("48", False),
    "NO": ("47", False), "DK": ("45", False), "MX": ("52", False),
    "BR": ("55", True), "IN": ("91", True), "ZA": ("27", True),
    "JP": ("81", True), "KR": ("82", True), "SG": ("65", False),
    "HK": ("852", False),
}
_NANP = frozenset({"US", "CA"})


def national_phone_to_e164(value, default_country):
    """Convert a phone number written without a leading ``+`` to E.164 using
    ``default_country``'s calling code. Never guesses: an unsupported or empty
    country, a ``+`` anywhere but the start, an ``00`` international prefix,
    or a digit count outside 8-15 (country code included) raises
    ``ValueError`` with a message for the row error. A number that does start
    with ``+`` is not this function's business -- use ``phone_e164``.
    """
    value = text(value)
    if not value:
        return value
    if value.startswith("+"):
        return phone_e164(value)
    country_code = (default_country or "").strip().upper()
    if not country_code:
        raise ValueError(
            "phone number has no leading + and no default country was chosen"
        )
    if country_code not in PHONE_COUNTRIES:
        raise ValueError(f"default phone country {country_code!r} is not supported")
    if "+" in value:
        raise ValueError("phone number has a + that is not at the start")
    digits = re.sub(r"\D", "", value)
    if digits.startswith("00"):
        raise ValueError(
            "phone number starts with 00; write international numbers with a leading +"
        )
    calling, trunk = PHONE_COUNTRIES[country_code]
    if country_code in _NANP:
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10 or digits[0] in "01":
            raise ValueError("phone number is not a valid 10-digit number")
    elif trunk and digits.startswith("0"):
        digits = digits[1:]
    full = calling + digits
    if not 8 <= len(full) <= 15:
        raise ValueError("phone number must have 8-15 digits including the country code")
    return "+" + full


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
                raise ValueError("an object key is not a string")
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
        raise ValueError("coordinate is not a valid number") from exc
    if d == 0:
        d = Decimal("0.000000")
    return d
