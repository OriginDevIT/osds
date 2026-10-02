"""The owner's leads page (decisions.md §4.9, §4.10): ``/owner/leads/``.

Behind ``owner_required``. Shows the non-spam leads on listings the owner holds,
newest first, with the visitor's details in full and escaped; another owner's
leads never appear; every view writes an access-log row.
"""

from __future__ import annotations

from datetime import timedelta
from django.test import Client
from django.utils import timezone

from audit.models import AccessLog
from directory import lead_notices
from directory.models import DirectoryUser, Lead, Listing
from directory.tests.test_claim_review import HOST, Role
from directory.tests.test_owner_auth import OWNER_EMAIL, _OwnerBase
from tenants.models import Tenant

OTHER_OWNER_EMAIL = "someone.else@elsewhere.example"
THEIRS = "ZZ-THEIR-SECRET-MESSAGE"


class _LeadsBase(_OwnerBase):
    def lead(self, listing=None, *, name="Priya R.", email="priya@example.test",
             phone="+13125550188", message="Burst pipe under the sink.", spam=False, age=None):
        lead = Lead.all_tenants.create(
            tenant=self.tenant, listing=listing or self.listing, kind="contact_form",
            name=name, email=email, phone_e164=phone, message=message, marked_spam=spam,
        )
        if age is not None:
            Lead.all_tenants.filter(pk=lead.pk).update(created_at=timezone.now() - age)
        return lead

    def other_owner(self, slug="other-biz"):
        other = DirectoryUser.all_tenants.create(
            tenant=self.tenant, email=OTHER_OWNER_EMAIL, name="Someone Else"
        )
        listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug=slug, name="Other Biz",
            visibility=Listing.Visibility.PUBLISHED, owner=other,
        )
        return other, listing

    def sign_in_as(self, email):
        client = Client()
        secret = self.issue(email)
        response = self.post(f"/owner/signin/{secret}/", client=client)
        assert response.status_code == 302, response.status_code
        return client

    def page(self, **extra):
        return self.get("/owner/leads/", **extra)

    def rows(self):
        return list(AccessLog.all_tenants.order_by("id"))


class AccessTests(_LeadsBase):
    def test_anonymous_is_sent_to_sign_in(self):
        r = self.page()
        self.assertEqual((r.status_code, r["Location"]), (302, "/owner/signin/"))
        self.assertEqual(self.rows(), [])

    def test_an_operator_session_does_not_open_it(self):
        client = Client()
        client.force_login(self.op(Role.ADMIN))
        r = self.get("/owner/leads/", client=client)
        self.assertEqual((r.status_code, r["Location"]), (302, "/owner/signin/"))
        self.assertEqual(self.rows(), [])

    def test_the_page_is_never_cached_or_indexed(self):
        self.sign_in()
        r = self.page()
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-cache", r["Cache-Control"])
        self.assertEqual(r["X-Robots-Tag"], "noindex, nofollow")


class ListingTests(_LeadsBase):
    def test_shows_every_detail_in_full_newest_first(self):
        self.sign_in()
        self.lead(name="Oldest", email="old@example.test", phone="", message="first", age=timedelta(days=3))
        self.lead(name="Newest", email="new@example.test", phone="+13125550188",
                  message="third and latest", age=timedelta(minutes=1))
        self.lead(name="Middle", email="mid@example.test", phone="", message="second", age=timedelta(days=1))

        body = self.page().content.decode()

        for text in ("new@example.test", "+13125550188", "third and latest",
                     "mid@example.test", "old@example.test", self.listing.name):
            self.assertIn(text, body)
        self.assertLess(body.index("Newest"), body.index("Middle"))
        self.assertLess(body.index("Middle"), body.index("Oldest"))

    def test_everything_a_visitor_typed_is_escaped(self):
        self.sign_in()
        self.lead(name="<b>bold</b>", message="<script>alert(1)</script>\nsecond line")
        body = self.page().content.decode()
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertNotIn("<b>bold</b>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertIn("second line", body)
        self.assertIn("<br>", body)  # newlines become line breaks, not markup

    def test_says_the_text_is_the_visitors_own(self):
        self.sign_in()
        self.assertContains(self.page(), "typed by them")

    def test_spam_is_not_shown(self):
        self.sign_in()
        self.lead(message="a real inquiry")
        self.lead(message="ZZ-SPAM-BODY", spam=True)
        body = self.page().content.decode()
        self.assertIn("a real inquiry", body)
        self.assertNotIn("ZZ-SPAM-BODY", body)

    def test_empty(self):
        self.sign_in()
        self.assertContains(self.page(), "No inquiries yet.")

    def test_pagination(self):
        self.sign_in()
        for i in range(51):
            self.lead(message=f"message number {i:02d}", age=timedelta(minutes=i))
        first = self.page().content.decode()
        second = self.get("/owner/leads/?page=2").content.decode()
        self.assertEqual(first.count("<article>"), 50)
        self.assertEqual(second.count("<article>"), 1)
        self.assertIn("message number 00", first)
        self.assertIn("message number 50", second)
        self.assertIn("Older", first)
        self.assertEqual(self.get("/owner/leads/?page=junk").status_code, 200)


class IsolationTests(_LeadsBase):
    def test_another_owners_leads_never_appear(self):
        other, theirs = self.other_owner()
        self.lead(message="mine")
        self.lead(listing=theirs, message=THEIRS, email="theirs@example.test")
        self.sign_in()

        body = self.page().content.decode()

        self.assertIn("mine", body)
        self.assertNotIn(THEIRS, body)
        self.assertNotIn("theirs@example.test", body)
        self.assertNotIn("Other Biz", body)

    def test_and_the_other_owner_sees_only_theirs(self):
        other, theirs = self.other_owner()
        self.lead(message="ZZ-MINE-ONLY")
        self.lead(listing=theirs, message=THEIRS)
        client = self.sign_in_as(OTHER_OWNER_EMAIL)

        body = self.get("/owner/leads/", client=client).content.decode()

        self.assertIn(THEIRS, body)
        self.assertNotIn("ZZ-MINE-ONLY", body)

    def test_an_unclaimed_listings_leads_are_nobodys_page(self):
        unclaimed = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="unclaimed", name="Unclaimed Co",
            visibility=Listing.Visibility.PUBLISHED,
        )
        self.lead(listing=unclaimed, message="ZZ-UNCLAIMED-BODY")
        self.sign_in()
        self.assertNotContains(self.page(), "ZZ-UNCLAIMED-BODY")

    def test_another_tenants_leads_never_appear(self):
        from directory.models import ListingType

        other_tenant = Tenant.objects.create(slug="other", name="Other", primary_domain="other.test")
        lt = ListingType.all_tenants.create(
            tenant=other_tenant, key="business", label_singular="B", label_plural="Bs",
            path_segment="businesses",
        )
        elsewhere_owner = DirectoryUser.all_tenants.create(tenant=other_tenant, email=OWNER_EMAIL)
        elsewhere = Listing.all_tenants.create(
            tenant=other_tenant, listing_type=lt, slug="elsewhere", name="Elsewhere",
            visibility=Listing.Visibility.PUBLISHED, owner=elsewhere_owner,
        )
        Lead.all_tenants.create(
            tenant=other_tenant, listing=elsewhere, kind="contact_form", name="X",
            email="x@example.test", message="ZZ-OTHER-TENANT-BODY",
        )
        self.sign_in()
        self.assertNotContains(self.page(), "ZZ-OTHER-TENANT-BODY")

    def test_ownership_is_read_live_so_a_listing_that_changes_hands_takes_its_leads(self):
        other, _ = self.other_owner()
        self.lead(message="ZZ-MOVES-WITH-THE-LISTING")
        self.sign_in()
        self.assertContains(self.page(), "ZZ-MOVES-WITH-THE-LISTING")

        Listing.all_tenants.filter(pk=self.listing.pk).update(owner=other)

        self.assertNotContains(self.page(), "ZZ-MOVES-WITH-THE-LISTING")


class AccessLogTests(_LeadsBase):
    def test_each_view_writes_one_row_naming_the_leads_shown(self):
        mine = self.lead(message="mine")
        spam = self.lead(message="spam", spam=True)
        _, theirs_listing = self.other_owner()
        theirs = self.lead(listing=theirs_listing, message=THEIRS)
        self.sign_in()

        self.page()
        self.page()

        rows = self.rows()
        self.assertEqual(len(rows), 2)
        row = rows[0]
        self.assertEqual(
            (row.action, row.resource_type, row.resource_id, row.tenant_id),
            ("viewed", "owner_leads", self.owner.public_id, self.tenant.pk),
        )
        self.assertEqual(row.actor, {"type": "owner", "id": self.owner.public_id})
        self.assertEqual(row.ip, "127.0.0.1")
        self.assertEqual(row.extra, {"page": 1, "lead_ids": [mine.public_id]})
        self.assertNotIn(spam.public_id, str(row.extra))
        self.assertNotIn(theirs.public_id, str(row.extra))

    def test_a_second_page_logs_its_own_page_and_leads(self):
        leads = [self.lead(message=f"m{i}", age=timedelta(minutes=i)) for i in range(51)]
        self.sign_in()
        self.get("/owner/leads/?page=2")
        row = self.rows()[-1]
        self.assertEqual(row.extra, {"page": 2, "lead_ids": [leads[50].public_id]})

    def test_an_empty_page_is_still_logged(self):
        self.sign_in()
        self.page()
        self.assertEqual(self.rows()[-1].extra, {"page": 1, "lead_ids": []})

    def test_nothing_visible_to_the_owner_is_logged_for_a_refused_request(self):
        self.page()
        self.assertEqual(self.rows(), [])


class DashboardLinkTests(_LeadsBase):
    def test_the_dashboard_links_to_the_page_with_the_count_of_real_inquiries(self):
        self.lead(message="one")
        self.lead(message="two")
        self.lead(message="spam", spam=True)
        _, theirs = self.other_owner()
        self.lead(listing=theirs, message=THEIRS)
        self.sign_in()
        r = self.get("/owner/")
        self.assertContains(r, 'href="/owner/leads/"')
        self.assertContains(r, "Inquiries (2)")

    def test_with_none_it_is_a_plain_link(self):
        self.sign_in()
        r = self.get("/owner/")
        self.assertContains(r, 'href="/owner/leads/"')
        self.assertContains(r, ">Inquiries</a>")
        self.assertNotContains(r, "Inquiries (")


class NoticeLinkTests(_LeadsBase):
    def test_the_owner_notice_points_at_the_page(self):
        from directory import leads as leads_module
        from directory.tests.lead_base import GRANTED
        from osds.tenancy import tenant_context

        Tenant.objects.filter(pk=self.tenant.pk).update(
            settings={**self.tenant.settings, "leads": {"enabled": True}},
            domain_verified_at=timezone.now(),
        )
        self.tenant.refresh_from_db()
        with tenant_context(self.tenant):
            leads_module.create_lead(
                self.tenant, listing=self.listing, kind="contact_form",
                contact={"name": "P", "email": "p@example.test", "phone_e164": ""},
                message="Please call me back about a quote.", consent=GRANTED,
                source_page="", ip=None,
            )
        from audit.models import OutboundMessage

        msg = OutboundMessage.all_tenants.get(kind=lead_notices.OWNER_NOTICE_KIND)
        self.assertEqual(msg.to_address, OWNER_EMAIL)
        self.assertIn("https://acme.test/owner/leads/", msg.body_text)
        self.assertNotIn("p@example.test", msg.body_text)
