"""Signing an outgoing webhook request (spec §8.4, decisions.md §4.12).

``X-OSDS-Signature: t=<unix seconds>,v1=<hex>``, where ``v1`` is HMAC-SHA256,
keyed with the tenant's signing secret, over ``"<t>.<raw body>"``.

Binding the timestamp into the signed string is what makes a captured request
useless later: a receiver recomputes the signature over exactly what it received
and refuses a ``t`` outside its tolerance. (The spec's §8.4 signs the raw body
alone; this adds the timestamp and is recorded as a deviation in decisions.md.)
Event ids are still the dedupe key (spec §8.2): delivery is at-least-once.

``verify`` is the reference receiver, and what the tests and ``docs/webhook.md``
show.
"""

from __future__ import annotations

import hashlib
import hmac

TOLERANCE_SECONDS = 300


def signature(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}.".encode("ascii") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def header(secret: str, timestamp: int, body: bytes) -> str:
    return f"t={timestamp},v1={signature(secret, timestamp, body)}"


def verify(
    secret: str, header_value: str, body: bytes, *, now: int, tolerance: int = TOLERANCE_SECONDS
) -> bool:
    """True if ``header_value`` is a valid signature of ``body`` made with
    ``secret``, no more than ``tolerance`` seconds from ``now``."""
    try:
        parts = dict(item.split("=", 1) for item in header_value.split(","))
        timestamp = int(parts["t"])
        offered = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(now - timestamp) > tolerance:
        return False
    return hmac.compare_digest(offered, signature(secret, timestamp, body))
