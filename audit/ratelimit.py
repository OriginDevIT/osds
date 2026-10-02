"""Fixed-window rate limits, counted in Postgres (#210, decisions.md §4.8).

No Redis, no cache backend, no new dependency: a counter is a row in
``rate_limit_counters`` keyed ``(tenant, rule, subject, window_start)`` and
incremented with one atomic ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING``.

* **Autocommit only.** A counter must survive the rollback of the command it
  guards, exactly as the command log does (spec §11.2), so ``check`` refuses to
  run inside an open transaction.
* **Nothing identifying is stored.** The subject (an IP bucket, a claimant
  email) is stored as a keyed hash -- Django's ``salted_hmac`` over
  ``SECRET_KEY``, the same mechanism as ``directory.claims.hash_code`` -- so the
  table holds no address a database reader could use. Rows are pruned a day
  after their window ends by the worker's ``rate_limit_prune`` job.
* **Fixed windows.** A client can burst up to twice a limit across a window
  boundary. These are blunt anti-abuse limits in front of precise per-pair
  guards (``ClaimVerificationGuard``), and the simplicity is worth it.
* **Short-circuit.** Checks are evaluated in order and stop at the first
  blocked rule, so a request already refused on its IP does not spend a
  victim's per-email budget.
* **Blocked attempts are logged once per window.** ``Verdict.first_block`` is
  true on exactly the hit that first crosses a limit, so a caller can write one
  ``blocked`` command-log row per window instead of one per request.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from math import ceil

from django.db import connection
from django.utils.crypto import salted_hmac

from audit.command_log import require_autocommit
from osds.client_ip import ip_bucket

_SALT = "osds.ratelimit"


@dataclass(frozen=True)
class Rule:
    """At most ``limit`` hits per ``window``. ``name`` is stored; renaming a
    rule starts a fresh count, which is harmless."""

    name: str
    limit: int
    window: timedelta


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    rule: "str | None" = None
    retry_after: int = 0  # whole seconds until the blocking window ends
    first_block: bool = False


class RateLimited(Exception):
    """A command was refused for arriving too often. Nothing was written but
    the counters and, once per window, a ``blocked`` command-log row."""

    def __init__(self, verdict: Verdict):
        self.verdict = verdict
        self.retry_after = verdict.retry_after
        self.rule = verdict.rule
        super().__init__(f"rate limited by {verdict.rule}; retry in {verdict.retry_after}s")


# (kind, raw value, rules). ``value`` of None or "" skips the group: a command
# with no client address (a management command, a test) has no IP to limit.
Check = "tuple[str, str | None, Sequence[Rule]]"


def subject_hash(tenant, kind: str, value: str) -> str:
    """The stored form of a subject: keyed, tenant-scoped, kind-scoped. An IP is
    reduced to its bucket first (IPv6 -> /64)."""
    if kind == "ip":
        value = ip_bucket(value)
    return salted_hmac(
        _SALT, f"{tenant.public_id}\0{kind}\0{value}", algorithm="sha256"
    ).hexdigest()


def _window_start(now: datetime, window: timedelta) -> datetime:
    seconds = int(window.total_seconds())
    start = (int(now.timestamp()) // seconds) * seconds
    return datetime.fromtimestamp(start, tz=dt_timezone.utc)


_UPSERT = """
INSERT INTO rate_limit_counters (tenant_id, rule, subject_hash, window_start, count)
VALUES (%s, %s, %s, %s, 1)
ON CONFLICT (tenant_id, rule, subject_hash, window_start)
DO UPDATE SET count = rate_limit_counters.count + 1
RETURNING count
"""


def _hit(tenant, rule: Rule, digest: str, now: datetime) -> "tuple[int, datetime]":
    start = _window_start(now, rule.window)
    with connection.cursor() as cursor:
        cursor.execute(_UPSERT, [tenant.pk, rule.name, digest, start])
        count = cursor.fetchone()[0]
    return count, start + rule.window


def check(tenant, *, checks: "Sequence[Check]", now: datetime) -> Verdict:
    """Count one hit against every rule of every group, in order, stopping at
    the first rule that is over its limit. ``now`` is the caller's clock."""
    require_autocommit()
    for kind, value, rules in checks:
        if not value:
            continue
        if kind == "ip" and not ip_bucket(value):
            continue
        digest = subject_hash(tenant, kind, value)
        for rule in rules:
            count, ends = _hit(tenant, rule, digest, now)
            if count > rule.limit:
                wait = max(1, ceil((ends - now).total_seconds()))
                return Verdict(
                    allowed=False,
                    rule=rule.name,
                    retry_after=wait,
                    first_block=count == rule.limit + 1,
                )
    return Verdict(allowed=True)
