"""The worker tick registry.

A ``TickRegistry`` is an *instance*, built once by the composition root
(``audit.worker.jobs.build_tick_registry``) and handed to the loop. There is
no module-level registry and no registration at import time: what a worker runs
is exactly what its registry was built with, and two registries never see each
other's jobs (#180).

``run_due`` is called on every worker pass. Cadence lives here, per job:
a job runs when ``now - last_run >= every``, so a daily retention sweep and a
30-second reindex drain share one loop without either knowing the other's
rhythm. ``last_run`` is stamped when a job is *attempted*, whether or not it
succeeds -- ``run_due`` runs every second, and a job that raises must wait out
its cadence, not be retried on every pass.

A job is ``fn(*, now)``. ``now`` is required and keyword-only: the clock is
the pass's single value, threaded through, never read inside a job. A job
returns ``None`` or a ``TickResult``. ``TickResult(more=True)`` says there is
backlog: the job stays due on the next pass and the worker does not idle. Every
job does a *bounded* amount of work per call, because the tick runs on the same
thread as the drains -- a long job would delay a claim-code email behind it.

Tick jobs are cross-tenant by nature and reach tenant-scoped models through
``all_tenants`` plus an explicit ``tenant_context``. That is the jobs' business;
this module imports no model and touches no table.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class TickResult:
    """What a job did. ``done`` counts units of work (rows, listings, tenants);
    ``more`` keeps the job due on the next pass."""

    done: int = 0
    more: bool = False


TickFn = Callable[..., "TickResult | None"]  # invoked as fn(*, now)


@dataclass(frozen=True)
class TickJob:
    name: str
    fn: TickFn
    every: timedelta


@dataclass
class TickStats:
    jobs_run: int = 0
    units_done: int = 0
    more: bool = False
    failures: "list[str]" = field(default_factory=list)


class TickRegistry:
    def __init__(self) -> None:
        self._jobs: "dict[str, TickJob]" = {}
        self._last_run: "dict[str, datetime]" = {}
        self._backlog: "set[str]" = set()

    @property
    def jobs(self) -> "list[TickJob]":
        return list(self._jobs.values())

    def register(self, name: str, fn: TickFn, *, every: timedelta) -> None:
        """Add a job. A repeated name raises: silently ignoring it is what left
        the first registration's bindings alive for the life of the process."""
        if name in self._jobs:
            raise ValueError(f"tick job {name!r} is already registered")
        if every <= timedelta(0):
            raise ValueError(f"tick job {name!r}: cadence must be positive")
        self._jobs[name] = TickJob(name=name, fn=fn, every=every)

    def _due(self, job: TickJob, now: datetime) -> bool:
        if job.name in self._backlog:
            return True
        last = self._last_run.get(job.name)
        return last is None or now - last >= job.every

    def run_due(self, *, now: datetime) -> TickStats:
        """Run every job that is due, in registration order.

        A job that raises ``Exception`` is recorded in ``TickStats.failures``
        and the rest still run -- one broken scheduled job must not stall the
        others. ``BaseException`` (a shutdown ``KeyboardInterrupt``) propagates.
        """
        stats = TickStats()
        for job in list(self._jobs.values()):
            if not self._due(job, now):
                continue
            self._last_run[job.name] = now
            self._backlog.discard(job.name)
            try:
                result = job.fn(now=now)
            except Exception as exc:  # recorded, not swallowed
                stats.failures.append(f"{job.name}: {type(exc).__name__}: {exc}")
                continue
            stats.jobs_run += 1
            if result is not None:
                stats.units_done += result.done
                if result.more:
                    self._backlog.add(job.name)
                    stats.more = True
        return stats
