"""The tick registry (#180): an instance, per-job cadence, failures, backlog.

Pure: no database. Every job here takes ``now`` as a required keyword, like the
real ones.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import SimpleTestCase

from audit.worker.jobs import build_tick_registry
from audit.worker.tick import TickRegistry, TickResult

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=dt_timezone.utc)
MIN = timedelta(minutes=1)


class RegistryTests(SimpleTestCase):
    def test_runs_every_due_job_once_in_order_with_now(self):
        seen = []
        reg = TickRegistry()
        reg.register("a", lambda *, now: seen.append(("a", now)), every=MIN)
        reg.register("b", lambda *, now: seen.append(("b", now)), every=MIN)

        stats = reg.run_due(now=T0)

        self.assertEqual(seen, [("a", T0), ("b", T0)])
        self.assertEqual(stats.jobs_run, 2)
        self.assertEqual(stats.failures, [])

    def test_a_duplicate_name_raises(self):
        reg = TickRegistry()
        reg.register("a", lambda *, now: None, every=MIN)
        with self.assertRaisesMessage(ValueError, "already registered"):
            reg.register("a", lambda *, now: None, every=MIN)

    def test_a_non_positive_cadence_raises(self):
        with self.assertRaises(ValueError):
            TickRegistry().register("a", lambda *, now: None, every=timedelta(0))

    def test_two_registries_share_nothing(self):
        one, two = TickRegistry(), TickRegistry()
        ran = []
        one.register("a", lambda *, now: ran.append("one"), every=MIN)
        two.register("a", lambda *, now: ran.append("two"), every=MIN)
        two.run_due(now=T0)
        self.assertEqual(ran, ["two"])

    def test_a_job_is_skipped_until_its_cadence_has_elapsed(self):
        ran = []
        reg = TickRegistry()
        reg.register("a", lambda *, now: ran.append(now), every=10 * MIN)

        reg.run_due(now=T0)
        reg.run_due(now=T0 + 9 * MIN)
        self.assertEqual(ran, [T0])
        reg.run_due(now=T0 + 10 * MIN)
        self.assertEqual(ran, [T0, T0 + 10 * MIN])

    def test_jobs_keep_independent_rhythms(self):
        ran = []
        reg = TickRegistry()
        reg.register("fast", lambda *, now: ran.append("fast"), every=MIN)
        reg.register("slow", lambda *, now: ran.append("slow"), every=10 * MIN)

        reg.run_due(now=T0)
        reg.run_due(now=T0 + 2 * MIN)
        self.assertEqual(ran, ["fast", "slow", "fast"])

    def test_a_failing_job_is_recorded_and_the_rest_still_run(self):
        ran = []
        reg = TickRegistry()
        reg.register("bad", lambda *, now: 1 / 0, every=MIN)
        reg.register("good", lambda *, now: ran.append(now), every=MIN)

        stats = reg.run_due(now=T0)

        self.assertEqual(ran, [T0])
        self.assertEqual(stats.jobs_run, 1)
        self.assertEqual(len(stats.failures), 1)
        self.assertIn("bad", stats.failures[0])
        self.assertIn("ZeroDivisionError", stats.failures[0])

    def test_a_failing_job_waits_out_its_cadence_instead_of_retrying_each_pass(self):
        calls = []

        def bad(*, now):
            calls.append(now)
            raise RuntimeError("boom")

        reg = TickRegistry()
        reg.register("bad", bad, every=10 * MIN)
        for second in range(5):
            reg.run_due(now=T0 + timedelta(seconds=second))
        self.assertEqual(len(calls), 1)

    def test_baseexception_from_a_job_propagates(self):
        def interrupted(*, now):
            raise KeyboardInterrupt

        reg = TickRegistry()
        reg.register("boom", interrupted, every=MIN)
        with self.assertRaises(KeyboardInterrupt):
            reg.run_due(now=T0)

    def test_more_keeps_a_job_due_on_the_next_pass(self):
        results = iter([TickResult(done=200, more=True), TickResult(done=50)])
        calls = []

        def job(*, now):
            calls.append(now)
            return next(results)

        reg = TickRegistry()
        reg.register("drain", job, every=30 * MIN)

        first = reg.run_due(now=T0)
        second = reg.run_due(now=T0 + timedelta(seconds=1))
        third = reg.run_due(now=T0 + timedelta(seconds=2))

        self.assertEqual(len(calls), 2)  # due again at +1s despite a 30min cadence
        self.assertTrue(first.more)
        self.assertFalse(second.more)
        self.assertEqual((first.units_done, second.units_done), (200, 50))
        self.assertEqual(third.jobs_run, 0)

    def test_a_job_that_raises_drops_its_backlog_flag(self):
        state = {"n": 0}

        def job(*, now):
            state["n"] += 1
            if state["n"] == 1:
                return TickResult(more=True)
            raise RuntimeError("boom")

        reg = TickRegistry()
        reg.register("j", job, every=30 * MIN)
        reg.run_due(now=T0)
        reg.run_due(now=T0 + timedelta(seconds=1))  # raises
        stats = reg.run_due(now=T0 + timedelta(seconds=2))
        self.assertEqual(state["n"], 2)
        self.assertEqual(stats.jobs_run, 0)


class BuiltRegistryTests(SimpleTestCase):
    def test_every_job_takes_now_as_a_required_keyword(self):
        # No job reads the clock itself: the pass's single `now` is the only one.
        import io

        for job in build_tick_registry(out=io.StringIO()).jobs:
            param = inspect.signature(job.fn).parameters["now"]
            self.assertIs(param.kind, inspect.Parameter.KEYWORD_ONLY, job.name)
            self.assertIs(param.default, inspect.Parameter.empty, job.name)
