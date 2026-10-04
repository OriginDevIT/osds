"""Stripe webhook signature verification, by hand (stdlib ``hmac``).

Stripe's documented scheme (docs.stripe.com/webhooks, "Verify manually"):

1. Split the ``Stripe-Signature`` header on ``,``, then each element on ``=``.
   ``t`` is the timestamp; each ``v1`` is a signature. **Every other scheme is
   ignored** -- ``v0`` exists only to aid testing, and honouring it would be a
   downgrade.
2. ``signed_payload`` is the timestamp as a string, a ``.``, then the raw body
   exactly as received.
3. The expected signature is HMAC-SHA256 over ``signed_payload`` with the
   endpoint's signing secret as the key, hex-encoded.
4. Compare it to **each** ``v1`` in constant time -- a rolled secret leaves two
   active for up to a day, and Stripe signs once per secret -- and require the
   timestamp to be within five minutes of now, which defeats replay of a
   captured request. A tolerance of zero would switch the check off, so the
   tolerance is a constant here and never configuration.

A body that has been parsed and re-serialised no longer matches its signature:
the caller must pass the bytes it received.
"""

from __future__ import annotations

import hashlib
import hmac

TOLERANCE_SECONDS = 300


class SignatureError(Exception):
    """The request is not provably from Stripe. The reason is a short code for
    the log; it is never sent back to the caller."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def parse_header(header: str) -> "tuple[int, list[str]]":
    """``(timestamp, [v1 signatures])`` or ``SignatureError``."""
    timestamp = None
    signatures: list[str] = []
    for element in (header or "").split(","):
        prefix, sep, value = element.strip().partition("=")
        if not sep:
            continue
        if prefix == "t":
            try:
                timestamp = int(value)
            except ValueError:
                raise SignatureError("bad_timestamp") from None
        elif prefix == "v1" and value:
            signatures.append(value)
    if timestamp is None:
        raise SignatureError("no_timestamp")
    if not signatures:
        raise SignatureError("no_v1_signature")
    return timestamp, signatures


def expected_signature(secret: str, timestamp: int, body: bytes) -> str:
    signed_payload = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()


def verify(header: str, body: bytes, secret: str, *, now: float) -> int:
    """Return the verified timestamp, or raise ``SignatureError``. ``now`` is
    the caller's clock, in seconds since the epoch."""
    timestamp, candidates = parse_header(header)
    expected = expected_signature(secret, timestamp, body)
    matched = False
    for candidate in candidates:  # no early exit: every v1 is compared
        if hmac.compare_digest(candidate.encode("ascii", "replace"), expected.encode("ascii")):
            matched = True
    if not matched:
        raise SignatureError("no_match")
    if abs(now - timestamp) > TOLERANCE_SECONDS:
        raise SignatureError("stale_timestamp")
    return timestamp
