"""The suppression-key fingerprint (spec §4.1.1, decisions.md "Removal sticks").

One implementation of the normalisation, and one only. The CSV importer checks
it before creating a listing; the future ``listing.deleted`` / hard-delete path
writes the row. **Both call ``fingerprint()`` with the same component
arguments** -- no caller ever joins the parts itself, because a second join is
exactly the drift issue #184 exists to prevent.

The normalisation here is effectively frozen: a ``SuppressionKey`` row stores
only the hash, and the listing it came from is deleted, so its components can
never be recovered to recompute. See the note above the pinning test in
``directory/tests/test_suppression.py``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WS = re.compile(r"\s+")
_PHONE_KEEP = re.compile(r"[^\d+]")
_LEADING_PLUSES = re.compile(r"^\++")

# Unit separator: control char, cannot survive _canon, so it cannot appear in a
# component and collide across the join.
_SEP = "\x1f"


def _canon(value) -> str:
    s = "" if value is None else str(value)
    s = unicodedata.normalize("NFKC", s)
    s = s.casefold()
    return _WS.sub(" ", s).strip()


def _canon_phone(value) -> str:
    s = unicodedata.normalize("NFKC", "" if value is None else str(value))
    cleaned = _PHONE_KEEP.sub("", s)
    if cleaned.startswith("+"):
        cleaned = "+" + _LEADING_PLUSES.sub("", cleaned)
    return cleaned


def fingerprint(
    *,
    name,
    address_line1=None,
    locality=None,
    region=None,
    postal_code=None,
    country=None,
    phone=None,
) -> str:
    """SHA-256 hex of a listing's normalised (name, address, phone).

    The address is the non-empty canonicalised components joined with ``", "``
    in this fixed order: line 1, locality, region, postal code, country.
    ``address_line2`` is deliberately excluded -- suite numbers are noisy and
    often absent. A missing component contributes nothing; a fully missing
    address is the empty string, as is a missing phone.

    Raises ``ValueError`` on a blank name: a name-less fingerprint would
    suppress every future blank row.
    """
    name_c = _canon(name)
    if not name_c:
        raise ValueError("fingerprint requires a non-empty name")

    address_c = ", ".join(
        part
        for part in (
            _canon(address_line1),
            _canon(locality),
            _canon(region),
            _canon(postal_code),
            _canon(country),
        )
        if part
    )
    phone_c = _canon_phone(phone)

    joined = _SEP.join([name_c, address_c, phone_c])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
