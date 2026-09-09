"""The outbox drain (worker PR 2).

The drain does two short transactions per attempt with the handler run
unlocked between them, so these tests use ``TransactionTestCase``. Time is
injected via ``now=``; jitter is handled with bounds and sampling.
"""

from __future__ import annotations

import ast
import itertools
import pathlib
from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

import audit.worker
from audit.models import CommandLog, OutboxDelivery, OutboxEvent
from audit.outbox import emit
from audit.worker import drain
from audit.worker.drain import (
    CLAIM_VISIBILITY_SECONDS,
    MAX_ATTEMPTS,
    attempt_delivery,
    backoff,
    drain_once,
    fan_out_once,
)
from osds.adapters import Result, override_subscribers
from osds.tenancy import get_current_tenant, tenant_context
from tenants.models import Tenant

HOUR = timedelta(hours=1)


# --------------------------------------------------------------------------
# test doubles
# --------------------------------------------------------------------------
class OkSubscriber:
    id = "webhook"

    def __init__(self):
        self.calls = []

    def handle(self, envelope):
        self.calls.append(envelope["id"])
        return Result.ok()


class FlakySubscriber:
    """Fails ``fail_times`` retryably per event id in ``flaky``, then succeeds.
    Records the ids it actually delivers, in order."""

    id = "webhook"

    def __init__(self, flaky, fail_times):
        self._left = {eid: fail_times for eid in flaky}
        self.delivered = []

    def handle(self, envelope):
        eid = envelope["id"]
        if self._left.get(eid, 0) > 0:
            self._left[eid] -= 1
            return Result.retry(1000, "flaky")
        self.delivered.append(eid)
        return Result.ok()


class AlwaysRetrySubscriber:
    """Fails ``doomed`` ids forever; succeeds everything else."""

    id = "webhook"

    def __init__(self, doomed):
        self._doomed = set(doomed)
        self.delivered = []

    def handle(self, envelope):
        if envelope["id"] in self._doomed:
            return Result.retry(1000, "still down")
        self.delivered.append(envelope["id"])
        return Result.ok()


class CrashThenOkSubscriber:
    """First call raises (simulating a worker dying mid-handler); then works."""

    id = "webhook"

    def __init__(self):
        self._crashed = False

    def handle(self, envelope):
        if not self._crashed:
            self._crashed = True
            raise SystemExit("worker killed mid-handler")
        return Result.ok()


class TenantSpySubscriber:
    id = "webhook"

    def __init__(self):
        self.seen = {}

    def handle(self, envelope):
        self.seen[envelope["subject"]] = get_current_tenant()
        return Result.ok()


class ScriptedSubscriber:
    """Returns a pre-set verb per call: ``ok`` | ``retry`` | ``permanent``."""

    id = "webhook"

    def __init__(self, script):
        self._script = list(script)
        self._i = 0

    def handle(self, envelope):
        verb = self._script[self._i]
        self._i += 1
        if verb == "ok":
            return Result.ok()
        if verb == "retry":
            return Result.retry(1000, "scripted retry")
        if verb == "permanent":
            return Result.failed("scripted permanent", permanent=True)
        raise AssertionError(f"unknown verb {verb!r}")


class KeyboardInterruptSubscriber:
    id = "webhook"

    def handle(self, envelope):
        raise KeyboardInterrupt("SIGINT during delivery")


# --------------------------------------------------------------------------
class _DrainBase(TransactionTestCase):
    def setUp(self):
        # The supported seam: an empty registry for the test, restored on
        # cleanup. ``self.subscribers`` is the live list.
        self.subscribers = self.enterContext(override_subscribers())
        notify = mock.patch("audit.outbox._notify_outbox")  # neither under test
        notify.start()
        self.addCleanup(notify.stop)

        self.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def register(self, subscriber):
        self.subscribers.append(subscriber)

    def emit(self, subject, *, tenant=None, etype="listing.updated"):
        return emit(etype, subject=subject, tenant=tenant or self.tenant)

    def delivery(self, event):
        return OutboxDelivery.all_tenants.get(event=event)


class PerSubjectOrderingTests(_DrainBase):
    def test_head_of_line_holds_a_subject_while_it_retries(self):
        sub = FlakySubscriber(flaky=[], fail_times=0)
        self.register(sub)
        a1 = self.emit("listing_S")
        a2 = self.emit("listing_S")
        b1 = self.emit("listing_T")
        sub._left = {a1.event_id: 2}  # a1 fails twice, then succeeds

        t0 = timezone.now()
        drain_once(now=t0)
        # b1 delivered immediately; a1 retrying; a2 blocked behind a1
        self.assertEqual(sub.delivered, [b1.event_id])
        self.assertEqual(self.delivery(a2).status, OutboxDelivery.Status.PENDING)
        self.assertEqual(self.delivery(a2).attempt, 0)

        drain_once(now=t0 + 2 * HOUR)  # a1 retry 2
        drain_once(now=t0 + 4 * HOUR)  # a1 succeeds
        drain_once(now=t0 + 6 * HOUR)  # a2 now head-of-line

        self.assertEqual(
            sub.delivered, [b1.event_id, a1.event_id, a2.event_id]
        )
        self.assertLess(
            sub.delivered.index(a1.event_id),
            sub.delivered.index(a2.event_id),
        )


class BackoffTests(SimpleTestCase):
    def test_first_step_within_one_second(self):
        for _ in range(50):
            self.assertLessEqual(backoff(1), timedelta(seconds=1))
            self.assertGreaterEqual(backoff(1), timedelta(0))

    def test_second_step_within_two_seconds(self):
        self.assertLessEqual(max(backoff(2) for _ in range(50)), timedelta(seconds=2))

    def test_capped_at_one_hour(self):
        for attempt in range(13, 30):
            for _ in range(20):
                self.assertLessEqual(backoff(attempt), timedelta(hours=1))

    def test_cap_is_one_hour_not_something_small(self):
        # uniform(0, 3600): P(all 200 samples below 3000s) ~ (3000/3600)**200 ~ 0
        biggest = max(backoff(20) for _ in range(200))
        self.assertGreater(biggest, timedelta(seconds=3000))

    def test_jittered(self):
        self.assertGreater(len({backoff(8) for _ in range(16)}), 1)


class ExhaustionTests(_DrainBase):
    def test_twelve_attempts_dead_letter_then_the_stream_unblocks(self):
        a1 = self.emit("listing_S")
        a2 = self.emit("listing_S")
        sub = AlwaysRetrySubscriber(doomed=[a1.event_id])
        self.register(sub)

        t0 = timezone.now()
        for i in range(MAX_ATTEMPTS):
            drain_once(now=t0 + (i + 1) * 2 * HOUR)

        d1 = self.delivery(a1)
        self.assertEqual(d1.status, OutboxDelivery.Status.DEAD)
        self.assertEqual(d1.attempt, MAX_ATTEMPTS)
        self.assertNotEqual(d1.last_error, "")
        # the full envelope is still reachable and replayable
        self.assertEqual(d1.event.data, {})
        self.assertEqual(d1.event.event_id, a1.event_id)

        # a2 was blocked the whole time; a dead a1 no longer blocks it
        self.assertEqual(self.delivery(a2).status, OutboxDelivery.Status.PENDING)
        drain_once(now=t0 + 100 * HOUR)
        self.assertEqual(self.delivery(a2).status, OutboxDelivery.Status.DELIVERED)
        self.assertEqual(sub.delivered, [a2.event_id])


class CrashedAttemptTests(_DrainBase):
    def test_a_crash_mid_handler_is_not_a_counted_attempt(self):
        sub = CrashThenOkSubscriber()
        self.register(sub)
        e = self.emit("listing_S")

        t0 = timezone.now()
        with self.assertRaises(SystemExit):
            drain_once(now=t0)

        d = self.delivery(e)
        self.assertEqual(d.status, OutboxDelivery.Status.PENDING)
        self.assertEqual(d.attempt, 0)
        self.assertIsNone(d.first_attempted_at)
        self.assertGreater(d.next_attempt_at, t0)  # visibility deadline pushed

        # recover once the deadline passes
        drain_once(now=t0 + timedelta(seconds=200))
        d.refresh_from_db()
        self.assertEqual(d.status, OutboxDelivery.Status.DELIVERED)
        self.assertEqual(d.attempt, 1)  # exactly one completed attempt


class NoSubscribersTests(_DrainBase):
    def test_event_with_no_subscribers_goes_straight_to_dispatched(self):
        e = self.emit("listing_S", etype="listing.created")

        fan_out_once()
        e.refresh_from_db()
        self.assertEqual(e.status, OutboxEvent.Status.DISPATCHED)
        self.assertIsNotNone(e.dispatched_at)
        self.assertEqual(
            OutboxDelivery.all_tenants.filter(event=e).count(), 0
        )

        stats = drain_once(now=timezone.now())
        self.assertEqual(stats.deliveries_attempted, 0)
        # nothing left to do on a second pass
        again = drain_once(now=timezone.now())
        self.assertEqual(again.events_fanned_out, 0)


class DrainSideEffectTests(_DrainBase):
    def test_drain_writes_no_command_log(self):
        self.register(OkSubscriber())
        self.emit("listing_S")
        self.emit("listing_T")
        drain_once(now=timezone.now())
        self.assertEqual(CommandLog.objects.count(), 0)

    def test_drain_emits_no_outbox_event_of_its_own(self):
        self.register(OkSubscriber())
        for subject in ("listing_S", "listing_T", "listing_U"):
            self.emit(subject)
        before = OutboxEvent.all_tenants.count()
        drain_once(now=timezone.now())
        self.assertEqual(OutboxEvent.all_tenants.count(), before)


class IdempotentFanOutTests(_DrainBase):
    def test_fan_out_is_idempotent_after_a_crash_before_the_status_flip(self):
        self.register(OkSubscriber())
        e = self.emit("listing_S")

        fan_out_once()
        self.assertEqual(
            OutboxDelivery.all_tenants.filter(event=e).count(), 1
        )

        # crash between row creation and the status flip
        OutboxEvent.all_tenants.filter(pk=e.pk).update(
            status=OutboxEvent.Status.PENDING
        )
        fan_out_once()

        self.assertEqual(
            OutboxDelivery.all_tenants.filter(event=e).count(), 1
        )
        e.refresh_from_db()
        self.assertEqual(e.status, OutboxEvent.Status.DISPATCHED)


class TwoTenantScopingTests(_DrainBase):
    def test_each_delivery_runs_in_its_own_tenant_context(self):
        t2 = Tenant.objects.create(slug="beta", name="Beta")
        spy = TenantSpySubscriber()
        self.register(spy)

        self.emit("listing_1", tenant=self.tenant)
        self.emit("listing_2", tenant=t2)

        stats = drain_once(now=timezone.now())

        self.assertEqual(stats.delivered, 2)
        self.assertEqual(spy.seen["listing_1"], self.tenant)
        self.assertEqual(spy.seen["listing_2"], t2)


class StaleRecordTests(_DrainBase):
    def test_stale_record_is_discarded(self):
        """A handler that hangs past its claim deadline has its row re-claimed
        and finished by later passes; when it finally records, the result
        lands on a moved target and is dropped -- no overwrite, no bad
        attempt count."""
        sub = ScriptedSubscriber(["retry", "ok", "retry"])
        self.register(sub)
        e = self.emit("listing_S")
        fan_out_once()
        d_pk = self.delivery(e).pk

        t0 = timezone.now()
        # Pass 1 claims the row; this attempt's (slow) handler runs last.
        claimed = drain._claim(t0, 100)
        self.assertEqual(claimed, [d_pk])
        slow = OutboxDelivery.all_tenants.select_related(
            "event", "event__tenant", "tenant"
        ).get(pk=d_pk)  # carries next_attempt_at == t0 + deadline

        # Pass 2, past the deadline: re-claims the row, attempt -> "retry".
        t1 = t0 + timedelta(seconds=CLAIM_VISIBILITY_SECONDS + 10)
        drain_once(now=t1)
        row = OutboxDelivery.all_tenants.get(pk=d_pk)
        self.assertEqual(row.status, OutboxDelivery.Status.PENDING)
        self.assertEqual(row.attempt, 1)

        # Pass 3, past pass 2's backoff: attempt -> "ok", delivered.
        drain_once(now=t1 + HOUR)
        row = OutboxDelivery.all_tenants.get(pk=d_pk)
        self.assertEqual(row.status, OutboxDelivery.Status.DELIVERED)
        self.assertEqual(row.attempt, 2)

        # Now the original slow attempt finally records -- two claims later.
        with tenant_context(slow.tenant):
            disposition = attempt_delivery(slow, now=t0)

        self.assertEqual(disposition, "discarded")
        row = OutboxDelivery.all_tenants.get(pk=d_pk)
        self.assertEqual(row.status, OutboxDelivery.Status.DELIVERED)  # kept
        self.assertEqual(row.attempt, 2)  # not decremented / re-counted


class KeyboardInterruptTests(_DrainBase):
    def test_keyboard_interrupt_mid_handler_leaves_row_claimable(self):
        """PR 3's shutdown path depends on KeyboardInterrupt propagating out
        of the drain, not being swallowed as a failed attempt."""
        self.register(KeyboardInterruptSubscriber())
        e = self.emit("listing_S")

        t0 = timezone.now()
        with self.assertRaises(KeyboardInterrupt):
            drain_once(now=t0)

        d = self.delivery(e)
        self.assertEqual(d.status, OutboxDelivery.Status.PENDING)
        self.assertEqual(d.attempt, 0)
        self.assertIsNone(d.first_attempted_at)
        self.assertIsNone(d.last_attempted_at)
        self.assertEqual(d.last_error, "")
        self.assertGreater(d.next_attempt_at, t0)  # only the deadline moved

        # once the deadline passes it is picked up again like any other row
        self.subscribers[:] = [OkSubscriber()]
        drain_once(now=t0 + timedelta(seconds=CLAIM_VISIBILITY_SECONDS + 5))
        d.refresh_from_db()
        self.assertEqual(d.status, OutboxDelivery.Status.DELIVERED)
        self.assertEqual(d.attempt, 1)


class ClockResolutionRegressionTests(_DrainBase):
    """A drain pass must be self-consistent whatever the clock's resolution --
    the CI failure was fan_out_once stamping ``next_attempt_at`` with a fresh
    ``timezone.now()`` strictly later than the ``now`` the same pass claims
    with (invisible on Windows's coarse clock, fatal on Linux's fine one)."""

    def test_fan_out_stamps_deliveries_with_the_injected_now(self):
        self.register(OkSubscriber())
        e = self.emit("listing_S")
        marker = timezone.now() + HOUR  # deliberately not wall-clock "now"

        fan_out_once(now=marker)

        self.assertEqual(self.delivery(e).next_attempt_at, marker)
        e.refresh_from_db()
        self.assertEqual(e.dispatched_at, marker)

    def test_drain_delivers_when_the_clock_never_repeats(self):
        real_now = timezone.now
        ticks = itertools.count(1)

        def strictly_monotonic():  # a fine-grained (Linux) clock
            return real_now() + timedelta(microseconds=next(ticks))

        self.register(OkSubscriber())
        self.emit("listing_S")
        with mock.patch("audit.worker.drain.timezone.now", strictly_monotonic):
            stats = drain_once(now=strictly_monotonic())

        self.assertEqual(stats.delivered, 1)


class WorkerSourceInvariantTests(SimpleTestCase):
    def _sources(self):
        root = pathlib.Path(audit.worker.__file__).parent
        return sorted(root.glob("*.py"))

    def test_worker_imports_no_adapter_code(self):
        for path in self._sources():
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotEqual(
                            alias.name.split(".")[0], "adapters", path
                        )
                elif isinstance(node, ast.ImportFrom):
                    head = (node.module or "").split(".")[0]
                    self.assertNotEqual(head, "adapters", path)

    def test_worker_uses_no_dot_objects(self):
        for path in self._sources():
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "objects":
                    self.fail(
                        f"{path}:{node.lineno} uses .objects; the worker must "
                        f"use all_tenants on tenant-scoped models"
                    )
