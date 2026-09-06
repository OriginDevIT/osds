"""Input canonicalisation for listing writes.

Applied to every incoming ``listing.upsert`` value so that a byte-identical
re-import produces an empty JSON Patch and no event (spec §7.1). An empty
string collapses to ``None`` -- "cleared" and "never set" are the same state
(ruling 8).
"""

from __future__ import annotations

import re
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
