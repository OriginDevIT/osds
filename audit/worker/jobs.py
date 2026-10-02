"""The composition root for tick jobs: which jobs a worker runs, and how often.

``build_tick_registry`` is the one place that knows the list. It is called from
``run_worker``'s ``handle`` and from tests; nothing registers at import time, so
there is no process-global state for a second ``handle()`` to inherit (#180).
Each job lives in its own domain module and is imported here, never the other
way round. Dependencies a job needs (an output stream) are bound with
``functools.partial``.

Cadences are chosen per job. The heartbeat and the sitemap fingerprint check
ride the default 60s; the reindex drain is cheap when idle and user-visible when
not, so it is faster; retention sweeps are daily.
"""

from __future__ import annotations

from datetime import timedelta
from functools import partial
from typing import IO

from audit.worker.loop import TICK_PERIOD
from audit.worker import retention
from audit.worker.tick import TickRegistry
from billing import jobs as billing_jobs
from directory import jobs as directory_jobs

REINDEX_EVERY = timedelta(seconds=30)
RETENTION_EVERY = timedelta(hours=24)
RATE_LIMIT_PRUNE_EVERY = timedelta(hours=1)
OWNER_AUTH_PRUNE_EVERY = timedelta(hours=1)
ENTITLEMENT_TRANSITIONS_EVERY = timedelta(minutes=5)
RENEWAL_NOTICES_EVERY = timedelta(hours=1)


def _heartbeat(out: IO[str], *, now) -> None:
    out.write(f"osds-worker: heartbeat {now.isoformat()}\n")


def build_tick_registry(*, out: IO[str]) -> TickRegistry:
    registry = TickRegistry()
    registry.register("heartbeat", partial(_heartbeat, out), every=TICK_PERIOD)
    registry.register(
        "search_reindex", directory_jobs.search_reindex, every=REINDEX_EVERY
    )
    registry.register(
        "sitemaps", directory_jobs.sitemap_regeneration, every=TICK_PERIOD
    )
    # Retention sweeps (spec §11.2): daily, idempotent, bounded per call.
    registry.register(
        "import_pre_image_retention",
        directory_jobs.import_pre_image_retention,
        every=RETENTION_EVERY,
    )
    registry.register(
        "outbound_address_retention",
        retention.null_outbound_addresses,
        every=RETENTION_EVERY,
    )
    registry.register(
        "event_payload_retention",
        retention.null_event_payloads,
        every=RETENTION_EVERY,
    )
    registry.register(
        "command_payload_retention",
        retention.null_command_payloads,
        every=RETENTION_EVERY,
    )
    registry.register(
        "rate_limit_prune",
        retention.prune_rate_limit_counters,
        every=RATE_LIMIT_PRUNE_EVERY,
    )
    # Owner sign-in links and sessions past their end (decisions.md §4.9).
    registry.register(
        "owner_auth_prune",
        directory_jobs.owner_auth_prune,
        every=OWNER_AUTH_PRUNE_EVERY,
    )
    # Entitlement transitions on the clock (spec §13; decisions.md §4.11).
    registry.register(
        "entitlement_transitions",
        billing_jobs.entitlement_transitions,
        every=ENTITLEMENT_TRANSITIONS_EVERY,
    )
    registry.register(
        "entitlement_renewal_notices",
        billing_jobs.renewal_notices,
        every=RENEWAL_NOTICES_EVERY,
    )
    return registry
