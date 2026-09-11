"""Contact-detail masking for public-facing claim UI (spec §9.4).

"We'll text +1 (773) 555-0142" turns a claim flow into a phone-number
disclosure endpoint for every listing on the site. Never render a listing's
phone or email where an unauthenticated visitor can read it in full; mask it
first. The bullet run is a fixed length regardless of the input's actual
size, so the mask itself never leaks how much is hidden.
"""

from __future__ import annotations

_MASK_RUN = "••••"  # fixed length; not one bullet per hidden digit

# The only calling codes with a single digit (NANP, Russia/Kazakhstan).
_SINGLE_DIGIT_CALLING_CODES = frozenset({"1", "7"})


def mask_phone_e164(phone: str) -> str:
    """Keep the leading "+", the country calling code, and the last two
    digits; everything between is one fixed-length bullet run. No NANP
    grouping -- this identifies the number without reading like one.

    Calling-code length can't be read off a bare E.164 string without a full
    ITU table, which isn't in the runtime tree (no phone-parsing dependency
    is). "1" and "7" are the only single-digit codes; every other number is
    assumed to have a 2-digit code. Guessing short is the safe direction to
    be wrong in -- it hides one extra real digit rather than exposing one
    that belongs to the subscriber number.
    """
    if not phone or not phone.startswith("+"):
        return phone
    digits = phone[1:]
    code_len = 1 if digits[:1] in _SINGLE_DIGIT_CALLING_CODES else 2
    if len(digits) < code_len + 2:
        return f"+{_MASK_RUN}"
    code = digits[:code_len]
    suffix = digits[-2:]
    return f"+{code}{_MASK_RUN}{suffix}"


def mask_email(email: str) -> str:
    """Keep the first character of the local part and the full domain;
    everything else in the local part is one fixed-length bullet run."""
    if not email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    if not local or not domain:
        return email
    return f"{local[0]}{_MASK_RUN}@{domain}"
