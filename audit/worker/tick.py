"""The worker tick registry.

``tick_once`` runs every registered tick job once, threading the pass's clock
through as ``now``. Cadence is not this module's concern -- ``worker_pass``
(in ``audit.worker.loop``) decides how often ``tick_once`` is called; a job
that wants a slower rhythm than the tick period tracks its own last-run time.

The registry ships empty. ``run_worker`` registers exactly one job, the
heartbeat, whose output goes through the command's ``stdout``. The scheduled
transitions in spec §13 -- dunning, grace and term expiry, T-10 renewal and
waitlist notices, slot-hold expiry, payload nulling, sitemap regeneration --
register here as their blocks land.

Nothing here imports adapter code, and nothing touches a tenant-scoped model.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

TickJob = Callable[..., None]  # invoked as job(*, now)

_JOBS: "list[tuple[str, TickJob]]" = []


@dataclass
class TickStats:
    jobs_run: int = 0
    failures: "list[str]" = field(default_factory=list)


def register_tick_job(name: str, fn: TickJob) -> None:
    """Register a tick job. Idempotent on ``name`` -- a repeat registration is
    ignored, so importing a module that registers is safe more than once."""
    if any(existing == name for existing, _ in _JOBS):
        return
    _JOBS.append((name, fn))


def tick_once(*, now) -> TickStats:
    """Run every registered tick job once, in registration order.

    A job that raises ``Exception`` is recorded in ``TickStats.failures`` and
    the remaining jobs still run -- one broken scheduled transition must not
    stall the others. ``BaseException`` (a shutdown ``KeyboardInterrupt``) is
    left to propagate.
    """
    stats = TickStats()
    for name, fn in list(_JOBS):
        try:
            fn(now=now)
        except Exception as exc:  # recorded in failures, not swallowed
            stats.failures.append(f"{name}: {type(exc).__name__}: {exc}")
        else:
            stats.jobs_run += 1
    return stats


@contextlib.contextmanager
def override_tick_jobs() -> "Iterator[list[tuple[str, TickJob]]]":
    """Install an empty tick registry for the duration of the block, restoring
    whatever was registered before. The registry is module state; this is the
    supported seam for tests, mirroring ``osds.adapters.override_subscribers``.
    """
    global _JOBS
    saved, _JOBS = _JOBS, []
    try:
        yield _JOBS
    finally:
        _JOBS = saved
