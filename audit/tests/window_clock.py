"""Pin the instant the rate limiter counts in, for tests that flood it.

Limits are fixed windows (decisions.md §4.8): a flood that straddles a window
boundary is counted in two windows and is not refused. A test that sends more
than a limit's worth of requests on the real clock therefore fails whenever a
slow runner carries it across a boundary (#245). ``pinned_windows`` makes every
core call site hand ``audit.ratelimit.check`` one fixed instant, so the whole
flood lands in one window however long it takes.

Only the limiter's input is pinned. ``timezone.now`` is left alone, so
sessions, token expiry and ``created_at`` keep the real clock. Move to a later
window with ``clock.advance(...)``.

``CALL_SITES`` is every module that imports ``check`` by name; a test fails if a
new one appears that is not listed here, so a future limit cannot quietly
escape the pin.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from audit import ratelimit

# Half past noon plus thirty seconds: well inside a 1-minute, 10-minute,
# 15-minute, 1-hour and 24-hour window alike.
PINNED = datetime(2026, 10, 4, 12, 35, 30, tzinfo=dt_timezone.utc)

CALL_SITES = (
    "billing.inbound.check",
    "billing.payments.check",
    "directory.claim_limits.check",
    "directory.lead_limits.check",
    "directory.owner_limits.check",
)


class WindowClock:
    def __init__(self, at: datetime):
        self.at = at

    def advance(self, **delta) -> None:
        self.at += timedelta(**delta)


@contextmanager
def pinned_windows(at: datetime = PINNED):
    clock = WindowClock(at)
    real = ratelimit.check

    def check(tenant, *, checks, now):
        return real(tenant, checks=checks, now=clock.at)

    with ExitStack() as stack:
        for site in CALL_SITES:
            stack.enter_context(mock.patch(site, check))
        yield clock
