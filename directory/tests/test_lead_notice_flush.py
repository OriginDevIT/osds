"""The lead-notice flush (decisions.md §4.10): a lead a throttle deferred is
announced once the window has passed, exactly once, and only when there is
something new to say.

Times: leads and notices are stamped with the real clock when created. The job
is driven with an explicit ``now`` -- a later one to stand for the window having
passed -- so nothing here sleeps.
"""

from __future__ import annotations

import io
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from audit.models import OutboundMessage
from audit.worker.jobs import build_tick_registry
from directory import jobs, lead_notices
from directory.models import Lead
from directory.tests.lead_base import LeadBase, Role
from tenants.models import Tenant

MIN = timedelta(minutes=1)


class _FlushBase(LeadBase):
    def flush(self, *, minutes_from_now):
        return jobs.lead_notice_flush(now=timezone.now() + minutes_from_now * MIN)

    def notices(self, kind=None, to=None):
        qs = OutboundMessage.all_tenants.order_by("id")
        if kind:
            qs = qs.filter(kind=kind)
        if to:
            qs = qs.filter(to_address=to)
        return qs


class OwnerFlushTests(_FlushBase):
    def setUp(self):
        super().setUp()
        self.owner = self.make_owner()
        self.owned = self.make_listing("owned", owner=self.owner, name="Owned Plumbing")

    def test_a_lead_inside_the_window_is_announced_after_it_with_no_later_lead(self):
        self.create(listing=self.owned)  # announced inline
        self.create(listing=self.owned)  # inside the window: throttled
        self.assertEqual(self.notices().count(), 1)

        early = self.flush(minutes_from_now=5)  # window still open
        self.assertEqual((early.done, self.notices().count()), (0, 1))

        late = self.flush(minutes_from_now=20)
        self.assertEqual((late.done, late.more), (1, False))
        flushed = self.notices().order_by("-id").first()
        self.assertEqual(flushed.kind, lead_notices.OWNER_NOTICE_KIND)
        self.assertEqual(flushed.to_address, self.owner.email)
        self.assertIn("You have 1 new inquiry on Owned Plumbing.", flushed.body_text)
        self.assertIn("https://acme.test/owner/", flushed.body_text)

    def test_the_count_covers_every_deferred_lead(self):
        for _ in range(4):
            self.create(listing=self.owned)  # 1 announced, 3 deferred
        self.flush(minutes_from_now=20)
        self.assertIn("3 new inquiries", self.notices().order_by("-id").first().body_text)

    def test_nothing_is_sent_twice(self):
        self.create(listing=self.owned)
        self.create(listing=self.owned)
        self.flush(minutes_from_now=20)
        sent = self.notices().count()
        for minutes in (20, 21, 40, 60):
            self.flush(minutes_from_now=minutes)
        self.assertEqual(self.notices().count(), sent)

    def test_nothing_is_sent_when_there_is_nothing_new(self):
        self.create(listing=self.owned)  # announced inline, nothing deferred
        result = self.flush(minutes_from_now=20)
        self.assertEqual((result.done, result.more), (0, False))
        self.assertEqual(self.notices().count(), 1)

    def test_nothing_is_sent_when_there_are_no_leads_at_all(self):
        self.assertEqual(self.flush(minutes_from_now=20).done, 0)
        self.assertFalse(self.notices().exists())

    def test_a_lead_after_a_flush_is_announced_inline_or_by_the_next_flush_once(self):
        self.create(listing=self.owned)
        self.create(listing=self.owned)
        self.flush(minutes_from_now=20)
        self.create(listing=self.owned)  # real clock: inside the new window
        self.flush(minutes_from_now=5)
        self.assertEqual(self.notices().count(), 2)
        self.flush(minutes_from_now=40)
        self.assertEqual(self.notices().count(), 3)
        self.flush(minutes_from_now=60)
        self.assertEqual(self.notices().count(), 3)

    def test_a_spam_lead_is_never_flushed(self):
        self.create(listing=self.owned)
        deferred = self.create(listing=self.owned)
        Lead.all_tenants.filter(pk=deferred.pk).update(marked_spam=True)
        self.assertEqual(self.flush(minutes_from_now=20).done, 0)
        self.assertEqual(self.notices().count(), 1)

    def test_a_lead_older_than_the_notice_lifetime_is_not_announced(self):
        lead = self.create(listing=self.owned)
        OutboundMessage.all_tenants.all().delete()  # it was never announced
        Lead.all_tenants.filter(pk=lead.pk).update(
            created_at=timezone.now() - timedelta(days=8)
        )
        self.assertEqual(self.flush(minutes_from_now=20).done, 0)
        self.assertFalse(self.notices().exists())

    def test_a_lead_that_was_never_announced_is_announced_once(self):
        # The mail row is gone (or the worker was down): the flush still tells
        # the owner, and only once.
        self.create(listing=self.owned)
        OutboundMessage.all_tenants.all().delete()
        self.assertEqual(self.flush(minutes_from_now=20).done, 1)
        self.assertEqual(self.flush(minutes_from_now=40).done, 0)

    def test_a_recipient_still_inside_their_window_is_left_alone(self):
        self.create(listing=self.owned)
        self.create(listing=self.owned)
        self.assertEqual(self.flush(minutes_from_now=14).done, 0)

    def test_no_visitor_text_reaches_a_flushed_notice(self):
        self.create(listing=self.owned)
        self.create(
            listing=self.owned,
            contact={"name": "ZZ-HOSTILE name", "email": "zz-hostile@evil.example", "phone_e164": ""},
            message="ZZ-HOSTILE pay me at http://evil.example",
        )
        self.flush(minutes_from_now=20)
        msg = self.notices().order_by("-id").first()
        for text in (msg.subject, msg.body_text):
            self.assertNotIn("HOSTILE", text.upper())
            self.assertNotIn("evil.example", text)

    def test_the_deadline_is_the_owner_notice_lifetime(self):
        self.create(listing=self.owned)
        self.create(listing=self.owned)
        now = timezone.now() + 20 * MIN
        jobs.lead_notice_flush(now=now)
        msg = self.notices().order_by("-id").first()
        self.assertEqual(msg.expires_at, now + lead_notices.OWNER_NOTICE_TTL)


class OperatorFlushTests(_FlushBase):
    def test_deferred_leads_on_unclaimed_listings_reach_the_editors(self):
        editor = self.op(Role.EDITOR)
        self.op(Role.MODERATOR)
        self.create()
        self.create()
        self.assertEqual(self.notices(to=editor.email).count(), 1)

        self.assertEqual(self.flush(minutes_from_now=5).done, 0)
        self.assertEqual(self.flush(minutes_from_now=20).done, 1)

        flushed = self.notices(to=editor.email).order_by("-id").first()
        self.assertEqual(flushed.kind, lead_notices.OPERATOR_NOTICE_KIND)
        self.assertIn("1 new inquiry arrived on listings that no one has claimed", flushed.body_text)
        self.assertIn("https://acme.test/admin/leads/", flushed.body_text)
        self.assertFalse(self.notices().filter(to_address__contains="op2@").exists())

    def test_nothing_is_sent_twice_or_when_nothing_is_new(self):
        editor = self.op(Role.EDITOR)
        self.create()
        self.assertEqual(self.flush(minutes_from_now=20).done, 0)  # nothing deferred
        self.create()
        self.flush(minutes_from_now=20)
        self.flush(minutes_from_now=40)
        self.assertEqual(self.notices(to=editor.email).count(), 2)

    def test_the_unclaimed_listings_own_address_and_the_visitor_are_never_mailed(self):
        self.op(Role.EDITOR)
        self.create(contact={"name": "P", "email": "visitor@example.test", "phone_e164": ""})
        self.create()
        self.flush(minutes_from_now=20)
        self.assertFalse(OutboundMessage.all_tenants.filter(
            to_address__in=["info@hoffmanplumbing.example", "visitor@example.test"]
        ).exists())

    def test_owned_listings_do_not_feed_the_operator_flush(self):
        editor = self.op(Role.EDITOR)
        owned = self.make_listing("owned", owner=self.make_owner())
        self.create(listing=owned)
        self.create(listing=owned)
        self.flush(minutes_from_now=20)
        self.assertFalse(self.notices(kind=lead_notices.OPERATOR_NOTICE_KIND, to=editor.email).exists())


class JobTests(_FlushBase):
    def test_a_suspended_tenant_is_skipped(self):
        owner = self.make_owner()
        owned = self.make_listing("owned", owner=owner)
        self.create(listing=owned)
        self.create(listing=owned)
        Tenant.objects.filter(pk=self.tenant.pk).update(status=Tenant.Status.SUSPENDED)
        self.assertEqual(self.flush(minutes_from_now=20).done, 0)

    def test_every_tenant_is_flushed_in_one_call(self):
        owner = self.make_owner()
        owned = self.make_listing("owned", owner=owner)
        self.create(listing=owned)
        self.create(listing=owned)

        from directory import leads
        from directory.models import DirectoryUser, Listing, ListingType
        from osds.tenancy import tenant_context

        other = Tenant.objects.create(
            slug="other", name="Other", primary_domain="other.test",
            domain_verified_at=timezone.now(), settings={"leads": {"enabled": True}},
        )
        lt = ListingType.all_tenants.create(
            tenant=other, key="business", label_singular="B", label_plural="Bs",
            path_segment="businesses",
        )
        other_owner = DirectoryUser.all_tenants.create(tenant=other, email="o@other.example")
        theirs = Listing.all_tenants.create(
            tenant=other, listing_type=lt, slug="theirs", name="Theirs",
            visibility=Listing.Visibility.PUBLISHED, owner=other_owner,
        )
        for i in range(2):
            with tenant_context(other):
                leads.create_lead(
                    other, listing=theirs, kind="contact_form",
                    contact={"name": "P", "email": f"p{i}@example.test", "phone_e164": ""},
                    message=f"Please call me back, number {i}.",
                    consent={"contact_by_business": {"granted": True}},
                    source_page="", ip=None,
                )

        result = self.flush(minutes_from_now=20)
        self.assertEqual(result.done, 2)
        recipients = set(
            OutboundMessage.all_tenants.order_by("id").filter(
                kind=lead_notices.OWNER_NOTICE_KIND
            ).values_list("to_address", flat=True)
        )
        self.assertEqual(recipients, {owner.email, other_owner.email})

    def test_a_backlog_is_bounded_and_the_job_stays_due_until_it_clears(self):
        owners = [self.make_owner(f"o{i}@hoffmanplumbing.example") for i in range(2)]
        for i, owner in enumerate(owners):
            listing = self.make_listing(f"owned-{i}", owner=owner)
            self.create(listing=listing)
            self.create(listing=listing)
        before = self.notices().count()

        with mock.patch.object(jobs, "LEAD_FLUSH_LIMIT", 1):
            first = self.flush(minutes_from_now=20)
            second = self.flush(minutes_from_now=20)
            third = self.flush(minutes_from_now=20)

        self.assertEqual(
            [(r.done, r.more) for r in (first, second, third)],
            [(1, True), (1, False), (0, False)],
        )
        self.assertEqual(self.notices().count(), before + 2)

    def test_it_is_registered_every_minute_and_takes_now_as_a_required_keyword(self):
        registry = build_tick_registry(out=io.StringIO())
        job = {j.name: j for j in registry.jobs}["lead_notice_flush"]
        self.assertEqual(job.every, timedelta(seconds=60))

    def test_the_registry_runs_it_with_the_passes_clock(self):
        owner = self.make_owner()
        owned = self.make_listing("owned", owner=owner)
        self.create(listing=owned)
        self.create(listing=owned)
        registry = build_tick_registry(out=io.StringIO())
        stats = registry.run_due(now=timezone.now() + 20 * MIN)
        self.assertEqual(stats.failures, [])
        self.assertEqual(
            OutboundMessage.all_tenants.filter(kind=lead_notices.OWNER_NOTICE_KIND).count(), 2
        )
