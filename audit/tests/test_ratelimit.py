"""audit.ratelimit (#210): fixed-window counters in Postgres.

``check`` refuses to run inside an open transaction (a counter must survive the
rollback of the command it guards), so these are TransactionTestCases. Every
clock is an explicit ``now``.
"""

from __future__ import annotations

import io
import threading
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.db import connection, transaction
from django.test import TransactionTestCase

from audit.command_log import MustNotBeInTransaction
from audit.models import RateLimitCounter
from audit.ratelimit import Rule, check, subject_hash
from audit.worker import retention
from audit.worker.jobs import RATE_LIMIT_PRUNE_EVERY, build_tick_registry
from tenants.models import Tenant

NOW = datetime(2026, 1, 1, 12, 3, 20, tzinfo=dt_timezone.utc)  # 3m20s into a 10m window
TEN_MIN = Rule("t.ten", 3, timedelta(minutes=10))
ONE_DAY = Rule("t.day", 5, timedelta(hours=24))
IP = "203.0.113.7"


class _Base(TransactionTestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")
        self.other = Tenant.objects.create(slug="other", name="Other")

    def hit(self, value=IP, *, kind="ip", rules=(TEN_MIN,), tenant=None, now=NOW):
        return check(tenant or self.tenant, checks=[(kind, value, rules)], now=now)


class CounterTests(_Base):
    def test_hits_up_to_the_limit_are_allowed_and_the_next_is_not(self):
        verdicts = [self.hit() for _ in range(4)]
        self.assertEqual([v.allowed for v in verdicts], [True, True, True, False])
        self.assertEqual(verdicts[3].rule, "t.ten")

    def test_first_block_is_true_only_on_the_hit_that_crosses_the_limit(self):
        verdicts = [self.hit() for _ in range(6)]
        self.assertEqual(
            [v.first_block for v in verdicts], [False, False, False, True, False, False]
        )

    def test_retry_after_is_the_time_to_the_end_of_the_window(self):
        for _ in range(3):
            self.hit()
        # NOW is 12:03:20; the 10-minute window ends 12:10:00 -> 400 seconds.
        self.assertEqual(self.hit().retry_after, 400)

    def test_a_new_window_starts_a_fresh_count(self):
        for _ in range(4):
            self.hit()
        later = NOW + timedelta(minutes=7)  # 12:10:20, the next window
        self.assertTrue(self.hit(now=later).allowed)

    def test_the_boundary_second_belongs_to_the_next_window(self):
        for _ in range(4):
            self.hit()
        edge = datetime(2026, 1, 1, 12, 10, 0, tzinfo=dt_timezone.utc)
        self.assertTrue(self.hit(now=edge).allowed)

    def test_tenants_do_not_share_counters(self):
        for _ in range(4):
            self.hit()
        self.assertFalse(self.hit().allowed)
        self.assertTrue(self.hit(tenant=self.other).allowed)

    def test_different_subjects_do_not_share_counters(self):
        for _ in range(4):
            self.hit()
        self.assertTrue(self.hit("198.51.100.9").allowed)

    def test_kinds_do_not_share_counters(self):
        for _ in range(4):
            self.hit("same")  # not an IP: skipped for kind=ip, so use email
        for _ in range(4):
            self.hit("same", kind="email")
        self.assertFalse(self.hit("same", kind="email").allowed)
        self.assertNotEqual(
            subject_hash(self.tenant, "email", "same"),
            subject_hash(self.tenant, "other", "same"),
        )

    def test_rules_in_a_group_count_independently(self):
        short = Rule("t.short", 2, timedelta(minutes=1))
        long = Rule("t.long", 4, timedelta(hours=1))
        results = [self.hit(rules=(short, long)).allowed for _ in range(3)]
        self.assertEqual(results, [True, True, False])
        # The third hit stopped at the short rule and never reached the long one.
        self.assertEqual(
            RateLimitCounter.all_tenants.get(rule="t.long").count, 2
        )

    def test_a_blocked_group_stops_before_the_next_group(self):
        for _ in range(3):
            check(
                self.tenant,
                checks=[("ip", IP, (TEN_MIN,)), ("email", "a@example.test", (ONE_DAY,))],
                now=NOW,
            )
        blocked = check(
            self.tenant,
            checks=[("ip", IP, (TEN_MIN,)), ("email", "a@example.test", (ONE_DAY,))],
            now=NOW,
        )
        self.assertFalse(blocked.allowed)
        # The email budget was spent three times, not four: a request refused on
        # its IP must not drain a victim's per-email allowance.
        self.assertEqual(RateLimitCounter.all_tenants.get(rule="t.day").count, 3)

    def test_an_absent_or_unusable_subject_is_skipped(self):
        for value in (None, "", "not-an-ip"):
            self.assertTrue(self.hit(value).allowed)
        self.assertFalse(RateLimitCounter.all_tenants.exists())

    def test_ipv6_addresses_share_a_counter_per_64(self):
        for i in range(3):
            self.hit(f"2001:db8:1:2::{i + 1}")
        self.assertFalse(self.hit("2001:db8:1:2:ffff::9").allowed)  # same /64
        self.assertTrue(self.hit("2001:db8:1:3::1").allowed)  # next /64

    def test_an_ipv4_mapped_ipv6_address_counts_as_the_ipv4(self):
        for _ in range(3):
            self.hit(IP)
        self.assertFalse(self.hit(f"::ffff:{IP}").allowed)

    def test_nothing_identifying_is_stored(self):
        email = "dana@hoffmanplumbing.example"
        self.hit(IP)
        self.hit(email, kind="email", rules=(ONE_DAY,))
        for row in RateLimitCounter.all_tenants.all():
            stored = " ".join(str(v) for v in (row.rule, row.subject_hash, row.window_start))
            self.assertNotIn("203.0.113", stored)
            self.assertNotIn("hoffman", stored)
            self.assertEqual(len(row.subject_hash), 64)

    def test_refuses_to_run_inside_a_transaction(self):
        with transaction.atomic():
            with self.assertRaises(MustNotBeInTransaction):
                self.hit()

    def test_the_count_survives_a_rolled_back_command(self):
        self.hit()
        try:
            with transaction.atomic():
                raise RuntimeError("the command failed")
        except RuntimeError:
            pass
        self.assertEqual(RateLimitCounter.all_tenants.get().count, 1)

    def test_concurrent_hits_are_counted_exactly(self):
        results, errors = [], []

        def worker():
            try:
                results.append(self.hit())
            except Exception as exc:  # surfaced below
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(RateLimitCounter.all_tenants.get().count, 20)
        self.assertEqual(sum(v.allowed for v in results), 3)
        self.assertEqual(sum(v.first_block for v in results), 1)


class PruneTests(_Base):
    def _row(self, age, *, tenant=None):
        return RateLimitCounter.all_tenants.create(
            tenant=tenant or self.tenant,
            rule="t.ten",
            subject_hash="a" * 64,
            window_start=NOW - age,
            count=1,
        )

    def test_deletes_counters_older_than_48_hours_and_keeps_the_rest(self):
        old = self._row(timedelta(hours=49))
        edge = self._row(timedelta(hours=48))
        recent = self._row(timedelta(hours=1), tenant=self.other)

        result = retention.prune_rate_limit_counters(now=NOW)

        self.assertEqual(result.done, 1)
        remaining = set(RateLimitCounter.all_tenants.values_list("pk", flat=True))
        self.assertEqual(remaining, {edge.pk, recent.pk})
        self.assertNotIn(old.pk, remaining)

    def test_is_idempotent(self):
        self._row(timedelta(hours=72))
        self.assertEqual(retention.prune_rate_limit_counters(now=NOW).done, 1)
        again = retention.prune_rate_limit_counters(now=NOW)
        self.assertEqual((again.done, again.more), (0, False))

    def test_is_bounded_and_reports_backlog(self):
        for i in range(3):
            self._row(timedelta(hours=72, minutes=i))
        with mock.patch.object(retention, "RETENTION_CHUNK", 2):
            first = retention.prune_rate_limit_counters(now=NOW)
            second = retention.prune_rate_limit_counters(now=NOW)
        self.assertEqual(
            [(first.done, first.more), (second.done, second.more)], [(2, True), (1, False)]
        )

    def test_is_registered_hourly(self):
        jobs = {j.name: j for j in build_tick_registry(out=io.StringIO()).jobs}
        self.assertEqual(jobs["rate_limit_prune"].every, RATE_LIMIT_PRUNE_EVERY)
        self.assertEqual(RATE_LIMIT_PRUNE_EVERY, timedelta(hours=1))
