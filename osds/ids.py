"""Public-facing identifiers.

ULIDs (Crockford base32, 26 characters), prefixed per entity so an id is
self-describing wherever it turns up -- a log line, a URL, an event envelope
(CLAUDE.md conventions). Integer primary keys stay internal.

No dependency is needed: a ULID is a 48-bit millisecond timestamp followed by
80 random bits, which is short enough to spell out.
"""

from __future__ import annotations

import os
import time

_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """A fresh 26-character ULID, lexicographically sortable by creation time
    to millisecond precision."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    out = bytearray(26)
    for i in range(25, -1, -1):
        out[i] = ord(_CROCKFORD32[value & 0x1F])
        value >>= 5
    return out.decode("ascii")


def _prefixed(prefix: str) -> str:
    return prefix + new_ulid()


# One factory per entity. Named module-level functions so Django migrations can
# serialise them as `osds.ids.<name>` field defaults.
def tnt_id() -> str:
    return _prefixed("tnt_")


def op_id() -> str:
    return _prefixed("op_")


def lt_id() -> str:
    return _prefixed("lt_")


def cat_id() -> str:
    return _prefixed("cat_")


def listing_id() -> str:
    return _prefixed("listing_")


def usr_id() -> str:
    return _prefixed("usr_")


def claim_id() -> str:
    return _prefixed("claim_")


def lead_id() -> str:
    return _prefixed("lead_")


def cns_id() -> str:
    return _prefixed("cns_")


def tier_id() -> str:
    return _prefixed("tier_")


def ent_id() -> str:
    return _prefixed("ent_")


def imp_id() -> str:
    return _prefixed("imp_")
