"""Who is told about a lead, and how (decisions.md §4.10): the owner or, with no
owner, the staff who can act -- never an unclaimed listing's own address, never
the visitor, and never visitor-supplied text.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from audit.models import OutboundMessage
from directory import lead_notices
from directory.models import Lead
from directory.tests.lead_base import LISTING_EMAIL, LeadBase, Role

# Anything typed into the form can carry this; it must never reach a notice.
HOSTILE = "ZZ-HOSTILE-MARKER"


class _NoticeBase(LeadBase):
    def hostile(self, **over):
        return self.create(
            contact={
                "name": f"{HOSTILE} name",
                "email": f"{HOSTILE.lower()}@evil.example",
                "phone_e164": "+13125550188",
            },
            message=f"{HOSTILE} pay me at http://evil.example",
            **over,
        )

    def notices(self, kind=None):
        qs = OutboundMessage.all_tenants.filter(tenant=self.tenant).order_by("id")
        return qs.filter(kind=kind) if kind else qs


class OwnerNoticeTests(_NoticeBase):
    def setUp(self):
        super().setUp()
        self.owner = self.make_owner()
        self.owned = self.make_listing("owned", owner=self.owner, name="Owned Plumbing")

    def test_the_owner_is_told_and_nobody_else(self):
        self.op(Role.ADMIN)  # staff are not mailed when there is an owner
        self.hostile(listing=self.owned)
        [msg] = self.notices()
        self.assertEqual(msg.kind, lead_notices.OWNER_NOTICE_KIND)
        self.assertEqual(msg.to_address, self.owner.email)
        self.assertEqual(msg.status, "pending")

    def test_the_message_carries_a_count_and_the_owner_link_but_no_visitor_text(self):
        self.hostile(listing=self.owned)
        msg = self.notices().get()
        self.assertEqual(msg.subject, f"New inquiry on {self.tenant.name}")
        self.assertIn("You have 1 new inquiry on Owned Plumbing.", msg.body_text)
        self.assertIn("https://acme.test/owner/", msg.body_text)
        for text in (msg.subject, msg.body_text):
            self.assertNotIn(HOSTILE, text)
            self.assertNotIn(HOSTILE.lower(), text)
            self.assertNotIn("evil.example", text)
            self.assertNotIn("+13125550188", text)

    def test_the_deadline_is_seven_days(self):
        before = timezone.now()
        self.create(listing=self.owned)
        msg = self.notices().get()
        self.assertAlmostEqual(
            (msg.expires_at - before).total_seconds(),
            lead_notices.OWNER_NOTICE_TTL.total_seconds(), delta=30,
        )
        self.assertEqual(lead_notices.OWNER_NOTICE_TTL, timedelta(days=7))

    def test_without_an_absolute_base_there_is_no_link(self):
        from tenants.models import Tenant

        Tenant.objects.filter(pk=self.tenant.pk).update(domain_verified_at=None)
        self.tenant.refresh_from_db()
        self.create(listing=self.owned)
        body = self.notices().get().body_text
        self.assertNotIn("http", body)
        self.assertIn("Sign in to your owner page", body)

    def test_a_second_lead_inside_fifteen_minutes_sends_nothing(self):
        self.create(listing=self.owned)
        self.create(listing=self.owned)
        self.assertEqual(self.notices().count(), 1)

    def test_the_count_includes_leads_skipped_while_throttled(self):
        first_lead = self.create(listing=self.owned)
        first = self.notices().get()
        self.create(listing=self.owned)  # throttled
        self.create(listing=self.owned)  # throttled
        # Age the first notice and the lead it announced together, as the clock
        # would have: a notice is created just after its lead.
        past = timezone.now() - timedelta(minutes=16)
        OutboundMessage.all_tenants.filter(pk=first.pk).update(created_at=past)
        Lead.all_tenants.filter(pk=first_lead.pk).update(created_at=past - timedelta(seconds=1))
        self.create(listing=self.owned)
        newest = self.notices().order_by("-id").first()
        self.assertIn("3 new inquiries", newest.body_text)

    def test_several_listings_are_summed_across_the_owners_listings(self):
        other = self.make_listing("owned-two", owner=self.owner, name="Second")
        self.create(listing=self.owned)
        OutboundMessage.all_tenants.update(created_at=timezone.now() - timedelta(minutes=16))
        self.create(listing=other)
        newest = self.notices().order_by("-id").first()
        self.assertIn("across your listings", newest.body_text)

    def test_a_spam_lead_tells_nobody(self):
        self.create(
            listing=self.owned,
            message="http://a.example http://b.example http://c.example aaaaaaaaaaaa",
        )
        self.assertFalse(self.notices().exists())

    def test_spam_is_not_counted_in_the_next_notice(self):
        self.create(
            listing=self.owned,
            message="http://a.example http://b.example http://c.example aaaaaaaaaaaa",
        )
        self.create(listing=self.owned)
        self.assertIn("1 new inquiry", self.notices().get().body_text)


class OperatorNoticeTests(_NoticeBase):
    def test_with_no_owner_the_editors_and_up_are_told(self):
        editor, manager, admin = (self.op(Role.EDITOR), self.op(Role.MANAGER), self.op(Role.ADMIN))
        self.op(Role.MODERATOR)
        self.op(Role.SUPPORT)
        self.op(Role.EDITOR, active=False)
        self.hostile()
        recipients = set(self.notices(lead_notices.OPERATOR_NOTICE_KIND).values_list("to_address", flat=True))
        self.assertEqual(recipients, {editor.email, manager.email, admin.email})

    def test_the_unclaimed_listings_own_address_is_never_mailed(self):
        self.op(Role.EDITOR)
        self.hostile()
        self.create()
        self.assertFalse(
            OutboundMessage.all_tenants.filter(to_address=LISTING_EMAIL).exists()
        )

    def test_the_visitor_is_never_mailed(self):
        self.op(Role.EDITOR)
        self.create(contact={"name": "Priya", "email": "visitor@example.test", "phone_e164": ""})
        self.assertFalse(
            OutboundMessage.all_tenants.filter(to_address="visitor@example.test").exists()
        )

    def test_the_message_is_a_count_with_the_admin_link_and_no_visitor_text(self):
        self.op(Role.EDITOR)
        self.hostile()
        msg = self.notices().get()
        self.assertIn("1 new inquiry arrived on listings that no one has claimed", msg.body_text)
        self.assertIn("https://acme.test/admin/leads/", msg.body_text)
        for text in (msg.subject, msg.body_text):
            self.assertNotIn(HOSTILE, text)
            self.assertNotIn("evil.example", text)

    def test_the_deadline_is_forty_eight_hours(self):
        self.op(Role.EDITOR)
        before = timezone.now()
        self.create()
        msg = self.notices().get()
        self.assertAlmostEqual(
            (msg.expires_at - before).total_seconds(), 48 * 3600, delta=30
        )

    def test_each_recipient_is_throttled_on_their_own(self):
        a, b = self.op(Role.EDITOR), self.op(Role.EDITOR)
        self.create()
        OutboundMessage.all_tenants.filter(to_address=a.email).update(
            created_at=timezone.now() - timedelta(minutes=16)
        )
        self.create()
        self.assertEqual(self.notices().filter(to_address=a.email).count(), 2)
        self.assertEqual(self.notices().filter(to_address=b.email).count(), 1)

    def test_an_owned_listing_does_not_count_toward_the_operator_figure(self):
        self.op(Role.EDITOR)
        owned = self.make_listing("owned", owner=self.make_owner())
        self.create(listing=owned)  # told the owner, not the staff
        self.create()
        staff = self.notices(lead_notices.OPERATOR_NOTICE_KIND).get()
        self.assertIn("1 new inquiry arrived", staff.body_text)

    def test_no_editors_means_no_mail_and_the_lead_is_still_captured(self):
        from directory.models import Lead

        self.op(Role.MODERATOR)
        self.create()
        self.assertFalse(self.notices().exists())
        self.assertEqual(Lead.all_tenants.count(), 1)

    def test_without_an_absolute_base_the_staff_message_has_no_link(self):
        from tenants.models import Tenant

        self.op(Role.EDITOR)
        Tenant.objects.filter(pk=self.tenant.pk).update(domain_verified_at=None)
        self.tenant.refresh_from_db()
        self.create()
        self.assertNotIn("http", self.notices().get().body_text)
