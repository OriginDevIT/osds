"""The tick jobs: entitlement transitions on the clock and the T-10 renewal
notice (spec §13; decisions.md §4.7, §4.11)."""

from __future__ import annotations

import io
from datetime import timedelta

from billing import jobs
from billing.machine import DUNNING, GRACE, Trigger
from billing.models import Entitlement
from billing.tests.base import DAY, T0, BillingTestCase, Role
from audit.worker.jobs import build_tick_registry
from directory.models import Listing
from tenants.models import Tenant

S = Entitlement.Status


class _Base(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.operator(Role.MANAGER)

    def seed_failed(self, listing, at):
        self.apply(Trigger.START, listing=listing, tier=self.featured, period_end=at - DAY, now=at - 30 * DAY)
        self.apply(Trigger.PAYMENT_FAILED, listing=listing, failure_code="x", now=at)


class TransitionJobTests(_Base):
    def test_walks_a_declined_card_from_dunning_through_grace_to_expiry(self):
        self.seed_failed(self.listing, T0)
        self.assertEqual(jobs.entitlement_transitions(now=T0 + DUNNING - timedelta(seconds=1)).done, 0)
        self.assertEqual(jobs.entitlement_transitions(now=T0 + DUNNING).done, 1)
        self.assertEqual(self.ent().status, S.GRACE)
        self.assertEqual(self.fresh().visibility, Listing.Visibility.PUBLISHED)
        self.assertEqual(jobs.entitlement_transitions(now=T0 + DUNNING + GRACE - timedelta(seconds=1)).done, 0)
        self.assertEqual(jobs.entitlement_transitions(now=T0 + DUNNING + GRACE).done, 1)
        self.assertEqual(self.ent().status, S.EXPIRED)
        listing = self.fresh()
        self.assertEqual(listing.current_tier, self.free)
        self.assertEqual(listing.visibility, Listing.Visibility.PUBLISHED)  # never unpublished

    def test_a_late_tick_never_skips_grace_and_grace_runs_from_when_it_began(self):
        self.seed_failed(self.listing, T0)
        far = T0 + DUNNING + GRACE + 10 * DAY  # the worker was down for weeks
        jobs.entitlement_transitions(now=far)
        self.assertEqual(self.ent().status, S.GRACE)
        jobs.entitlement_transitions(now=far)  # grace has only just begun
        self.assertEqual(self.ent().status, S.GRACE)
        self.assertEqual(self.ent().grace_ends_at, far + GRACE)
        jobs.entitlement_transitions(now=far + GRACE)
        self.assertEqual(self.ent().status, S.EXPIRED)

    def test_every_clock_driven_move_is_picked_up(self):
        past = T0 - timedelta(seconds=1)
        a = self.make_listing("canc", "Canc")
        self.apply(Trigger.START, listing=a, tier=self.featured, period_end=past, now=T0 - 30 * DAY)
        self.apply(Trigger.CANCEL, listing=a, canceled_by="owner", now=T0 - 20 * DAY)
        b = self.make_listing("comp", "Comp")
        self.apply(Trigger.GRANT, listing=b, tier=self.featured, operator=self.admin, reason="r",
                   expires_at=past, now=T0 - 30 * DAY)
        c = self.make_listing("term", "Term")
        self.apply(Trigger.START, listing=c, tier=self.featured, period_end=past, now=T0 - 30 * DAY,
                   billing_mode="term", term_days=30)
        d = self.make_listing("trial", "Trial")
        self.apply(Trigger.START_TRIAL, listing=d, tier=self.featured,
                   trial_ends_at=T0 - 4 * DAY, now=T0 - 11 * DAY)
        result = jobs.entitlement_transitions(now=T0)
        self.assertEqual(result.done, 4)
        self.assertEqual(self.ent(a).status, S.EXPIRED)
        self.assertEqual(self.ent(b).status, S.EXPIRED)
        self.assertEqual(self.ent(c).status, S.EXPIRED)
        self.assertEqual(self.ent(d).status, S.PAST_DUE)

    def test_a_row_that_is_not_due_is_left_alone(self):
        self.apply(Trigger.START, tier=self.featured, period_end=T0 + 30 * DAY, now=T0)
        since = self.last_event_id()
        self.assertEqual(jobs.entitlement_transitions(now=T0 + DAY).done, 0)
        self.assertEqual(self.events(since=since).count(), 0)

    def test_a_payment_that_landed_first_wins(self):
        self.seed_failed(self.listing, T0)
        # The row looks due to the query, but a payment recovered it before the
        # job's own re-check under the lock.
        self.apply(Trigger.PAYMENT_SUCCEEDED, now=T0 + DUNNING - DAY, period_end=T0 + 60 * DAY)
        Entitlement.all_tenants.filter(listing=self.listing).update(
            dunning_ends_at=T0, status=S.ACTIVE
        )
        self.assertEqual(jobs.entitlement_transitions(now=T0 + DUNNING).done, 0)
        self.assertEqual(self.ent().status, S.ACTIVE)

    def test_it_is_idempotent(self):
        self.seed_failed(self.listing, T0)
        jobs.entitlement_transitions(now=T0 + DUNNING)
        since = self.last_event_id()
        self.assertEqual(jobs.entitlement_transitions(now=T0 + DUNNING).done, 0)
        self.assertEqual(self.events(since=since).count(), 0)

    def test_it_is_bounded_and_reports_backlog(self):
        for i in range(3):
            self.seed_failed(self.make_listing(f"b{i}", f"B{i}"), T0)
        original = jobs.CHUNK
        jobs.CHUNK = 2
        try:
            first = jobs.entitlement_transitions(now=T0 + DUNNING)
            second = jobs.entitlement_transitions(now=T0 + DUNNING)
        finally:
            jobs.CHUNK = original
        self.assertEqual((first.done, first.more), (2, True))
        self.assertEqual((second.done, second.more), (1, False))

    def test_it_works_across_tenants(self):
        other_tenant = Tenant.objects.create(slug="other", name="Other", primary_domain="other.test")
        from billing.models import Tier
        from directory.models import ListingType

        lt = ListingType.all_tenants.create(
            tenant=other_tenant, key="b", label_singular="B", label_plural="Bs", path_segment="bs"
        )
        tier = Tier.all_tenants.create(tenant=other_tenant, key="top", name="Top", rank=1)
        other_listing = Listing.all_tenants.create(
            tenant=other_tenant, listing_type=lt, slug="x", name="X",
            visibility=Listing.Visibility.PUBLISHED,
        )
        from billing import entitlements
        from osds.tenancy import tenant_context

        with tenant_context(other_tenant):
            entitlements.apply_trigger(other_tenant, other_listing, Trigger.START, now=T0 - 30 * DAY,
                                       tier=tier, period_end=T0 - DAY)
            entitlements.apply_trigger(other_tenant, other_listing, Trigger.PAYMENT_FAILED,
                                       now=T0 - DUNNING, failure_code="x")
        self.seed_failed(self.listing, T0 - DUNNING)
        self.assertEqual(jobs.entitlement_transitions(now=T0).done, 2)
        self.assertEqual(Entitlement.all_tenants.get(listing=other_listing).status, S.GRACE)
        self.assertEqual(self.ent().status, S.GRACE)

    def test_it_is_registered_with_the_workers_registry_and_takes_now_by_keyword(self):
        registry = build_tick_registry(out=io.StringIO())
        names = [job.name for job in registry.jobs]
        self.assertIn("entitlement_transitions", names)
        self.assertIn("entitlement_renewal_notices", names)


class RenewalNoticeTests(_Base):
    def term(self, listing, end, **kw):
        self.apply(Trigger.START, listing=listing, tier=self.featured, period_end=end, now=T0 - 20 * DAY,
                   billing_mode="term", term_days=30)

    def test_ten_days_out_it_emits_renewal_due_once_per_period(self):
        self.term(self.listing, T0 + 10 * DAY)
        self.assertEqual(jobs.renewal_notices(now=T0 - timedelta(seconds=1)).done, 0)
        self.assertEqual(jobs.renewal_notices(now=T0).done, 1)
        data = self.events("entitlement.renewal_due").get().data
        self.assertEqual((data["days_remaining"], data["term_days"]), (10, 30))
        self.assertIsNotNone(self.ent().renewal_notified_at)
        self.assertEqual(jobs.renewal_notices(now=T0 + DAY).done, 0)
        self.assertEqual(self.events("entitlement.renewal_due").count(), 1)

    def test_a_renewal_resets_the_notice_for_the_next_period(self):
        self.term(self.listing, T0 + 10 * DAY)
        jobs.renewal_notices(now=T0)
        self.apply(Trigger.PAYMENT_SUCCEEDED, now=T0 + 2 * DAY, period_end=T0 + 40 * DAY)
        self.assertIsNone(self.ent().renewal_notified_at)

    def test_recurring_and_ended_entitlements_get_no_notice(self):
        self.apply(Trigger.START, tier=self.featured, period_end=T0 + 5 * DAY, now=T0 - DAY)
        other = self.make_listing("ended", "Ended")
        self.term(other, T0 - DAY)
        self.assertEqual(jobs.renewal_notices(now=T0).done, 0)
