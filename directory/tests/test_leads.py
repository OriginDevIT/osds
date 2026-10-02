"""directory.leads: the lead.create and lead.mark_spam commands (spec §3.3, §7,
§9.0; decisions.md §4.10).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone

from audit.models import CommandLog, OutboundMessage, OutboxEvent, RateLimitCounter
from audit.ratelimit import RateLimited, Rule
from directory import lead_limits, lead_notices, leads
from directory.field_schema import SchemaError
from directory.models import Consent, ConsentText, Lead, Listing, ListingType
from directory.services import ConsentRequired
from directory.tests.lead_base import GOOD_MESSAGE, GRANTED, LeadBase, Role
from osds.tenancy import tenant_context
from tenants.models import Tenant

IP = "198.51.100.7"


def log(command="lead.create", **kw):
    return CommandLog.objects.filter(command=command, **kw)


class CreateLeadTests(LeadBase):
    def test_creates_the_lead_its_consent_and_the_event(self):
        lead = self.create(
            contact={"name": "  Priya R.  ", "email": "Priya@Example.TEST", "phone_e164": "+1 (312) 555-0188"},
            message="Burst pipe under the sink,\r\nneed someone today.",
        )
        self.assertEqual(lead.name, "Priya R.")
        self.assertEqual(lead.email, "priya@example.test")
        self.assertEqual(lead.phone_e164, "+13125550188")
        self.assertEqual(lead.message, "Burst pipe under the sink,\nneed someone today.")
        self.assertEqual(lead.kind, "contact_form")
        self.assertEqual(lead.status, "captured")
        self.assertFalse(lead.marked_spam)
        self.assertEqual(lead.spam_score, Decimal("0.000"))
        self.assertEqual(lead.source_page, "/plumbers/hoffman-plumbing")
        self.assertTrue(lead.public_id.startswith("lead_"))

        consent = Consent.all_tenants.get(lead=lead)
        self.assertEqual(
            (consent.channel, consent.granted, consent.ip, consent.text_version),
            ("contact_by_business", True, IP, "lead-consent-v1"),
        )
        self.assertIsNone(consent.claim_id)
        self.assertIsNotNone(consent.granted_at)

    def test_the_event_has_the_spec_shape(self):
        lead = self.create(
            contact={"name": "Priya R.", "email": "priya@example.test", "phone_e164": "+13125550188"},
            message=GOOD_MESSAGE,
        )
        event = OutboxEvent.all_tenants.get(type="lead.captured")
        self.assertEqual(event.subject, lead.public_id)
        self.assertEqual(event.actor, {"type": "visitor", "id": ""})
        data = event.data
        self.assertEqual(
            data["lead"],
            {
                "id": lead.public_id, "kind": "contact_form", "name": "Priya R.",
                "email": "priya@example.test", "phone_e164": "+13125550188",
                "message": GOOD_MESSAGE, "spam_score": 0.0,
            },
        )
        self.assertEqual(data["listing_id"], self.listing.public_id)
        self.assertEqual(data["source_page"], "/plumbers/hoffman-plumbing")
        entry = data["consent"]["contact_by_business"]
        self.assertEqual((entry["granted"], entry["ip"], entry["text_version"]), (True, IP, "lead-consent-v1"))
        self.assertTrue(entry["at"])

    def test_the_command_log_records_the_attempt_outside_the_command(self):
        lead = self.create()
        row = log().get()
        self.assertEqual(row.outcome, "applied")
        self.assertEqual(row.actor, {"type": "visitor", "id": ""})
        self.assertEqual(row.payload["lead"]["message"], lead.message)
        self.assertEqual(row.payload["listing_id"], self.listing.public_id)
        self.assertEqual(
            row.result_event_id, OutboxEvent.all_tenants.get(type="lead.captured").event_id
        )

    def test_no_directory_user_is_minted(self):
        from directory.models import DirectoryUser

        self.create()
        self.assertFalse(DirectoryUser.all_tenants.exists())

    def test_the_consent_wording_is_seeded_once_and_is_neutral(self):
        self.create()
        self.create()
        text = ConsentText.all_tenants.get(tenant=self.tenant, key="lead-consent")
        self.assertEqual(text.version, "v1")
        self.assertEqual(text.body, leads.DEFAULT_LEAD_CONSENT_BODY)
        self.assertNotIn("PLACEHOLDER", text.body)

    def test_an_unparseable_ip_is_not_stored(self):
        self.create(ip="unix:")
        self.assertIsNone(Consent.all_tenants.get().ip)

    def test_a_missing_ip_is_allowed(self):
        self.create(ip=None)
        self.assertIsNone(Consent.all_tenants.get().ip)


class RefusalTests(LeadBase):
    def refused(self, exc, **over):
        with self.assertRaises(exc) as cm:
            self.create(**over)
        self.assertFalse(Lead.all_tenants.exists())
        self.assertFalse(Consent.all_tenants.exists())
        self.assertFalse(OutboxEvent.all_tenants.filter(type="lead.captured").exists())
        return cm.exception

    def test_missing_consent_is_rejected_and_logged(self):
        exc = self.refused(ConsentRequired, consent={})
        self.assertEqual(exc.channel, "contact_by_business")
        row = log().get()
        self.assertEqual((row.outcome, row.problem), ("rejected", {"missing_consent": "contact_by_business"}))

    def test_declined_consent_is_rejected_too(self):
        self.refused(ConsentRequired, consent={"contact_by_business": {"granted": False}})
        self.assertEqual(log().get().outcome, "rejected")

    def test_malformed_consent_is_rejected(self):
        self.refused(ConsentRequired, consent={"contact_by_business": True})

    def test_a_disabled_tenant_is_refused(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(settings={})
        self.tenant.refresh_from_db()
        self.refused(SchemaError)
        self.assertEqual(log().get().outcome, "rejected")

    def test_an_unpublished_listing_is_refused(self):
        Listing.all_tenants.filter(pk=self.listing.pk).update(visibility="draft")
        self.refused(SchemaError)

    def test_a_suspended_listing_is_refused(self):
        Listing.all_tenants.filter(pk=self.listing.pk).update(status="suspended")
        self.refused(SchemaError)

    def test_malformed_input_is_rejected_before_any_counter_moves(self):
        for over in (
            {"contact": {"name": "P", "email": "", "phone_e164": ""}},
            {"contact": {"name": "", "email": "p@example.test", "phone_e164": ""}},
            {"contact": {"name": "P", "email": "p@example.test", "phone_e164": "12"}},
            {"message": "too short"},
            {"message": "x" * (leads.MESSAGE_MAX + 1)},
            {"kind": "carrier_pigeon"},
        ):
            with self.subTest(over=over):
                self.refused(SchemaError, **over)
        self.assertFalse(RateLimitCounter.all_tenants.exists())
        self.assertTrue(all(r.outcome == "rejected" for r in log()))

    def test_control_characters_are_stripped_and_blank_runs_collapsed(self):
        lead = self.create(message="Hello\x00 there\x07, please call\n\n\n\n\nme back soon.")
        self.assertEqual(lead.message, "Hello there, please call\n\nme back soon.")

    def test_a_failure_mid_apply_rolls_everything_back_and_leaves_the_log_open(self):
        with mock.patch.object(lead_notices, "notify_for_lead", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertFalse(Lead.all_tenants.exists())
        self.assertFalse(Consent.all_tenants.exists())
        self.assertFalse(OutboxEvent.all_tenants.exists())
        row = log().get()
        self.assertIsNone(row.outcome)  # spec §11.2: threw mid-apply, that is the record


class DuplicateTests(LeadBase):
    def same(self):
        return dict(
            contact={"name": "Priya", "email": "priya@example.test", "phone_e164": ""},
            message="Please call me about the burst pipe.",
        )

    def test_a_repeat_within_the_hour_returns_the_first_lead_and_does_nothing(self):
        first = self.create(**self.same())
        second = self.create(**self.same())
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Lead.all_tenants.count(), 1)
        self.assertEqual(Consent.all_tenants.count(), 1)
        self.assertEqual(OutboxEvent.all_tenants.filter(type="lead.captured").count(), 1)
        rows = list(log().order_by("id"))
        self.assertEqual(rows[1].problem, {"duplicate_of": first.public_id})
        self.assertEqual(rows[1].outcome, "applied")

    def test_a_repeat_after_the_window_is_a_new_lead(self):
        first = self.create(**self.same())
        Lead.all_tenants.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(minutes=61))
        second = self.create(**self.same())
        self.assertNotEqual(first.pk, second.pk)

    def test_a_different_message_or_listing_is_not_a_duplicate(self):
        first = self.create(**self.same())
        other_listing = self.make_listing("second-plumber")
        a = self.create(listing=other_listing, **self.same())
        b = self.create(**{**self.same(), "message": "A completely different question."})
        self.assertEqual(len({first.pk, a.pk, b.pk}), 3)


class SpamScoreTests(LeadBase):
    def score(self, *, name="Priya", email="p@example.test", message=GOOD_MESSAGE):
        return leads.spam_score(name=name, email=email, message=message)

    def test_a_clean_message_scores_zero(self):
        v = self.score()
        self.assertEqual((v.score, v.signals), (Decimal("0.000"), ()))

    def test_each_signal_has_its_weight(self):
        self.assertEqual(self.score(message="see http://a.example for more").score, Decimal("0.350"))
        self.assertEqual(self.score(message="PLEASE CALL ME BACK ABOUT MY PIPE NOW").score, Decimal("0.200"))
        self.assertEqual(self.score(message="please help meeeeeeeeee now").score, Decimal("0.200"))
        self.assertEqual(self.score(name="bob@spam.example").score, Decimal("0.300"))

    def test_links_are_capped_and_the_total_is_clamped(self):
        v = self.score(name="www.x.example", message="http://a http://b http://c http://d AAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(v.score, Decimal("1.000"))
        self.assertIn("links_in_message", v.signals)

    def test_a_spammy_lead_is_stored_marked_and_announced_but_not_delivered(self):
        owner = self.make_owner()
        listing = self.make_listing("owned", owner=owner)
        lead = self.create(
            listing=listing,
            message="Buy now http://a.example http://b.example http://c.example aaaaaaaaaaaa",
        )
        self.assertTrue(lead.marked_spam)
        self.assertGreaterEqual(lead.spam_score, leads.SPAM_THRESHOLD)
        types = list(OutboxEvent.all_tenants.order_by("id").values_list("type", flat=True))
        self.assertEqual(types, ["lead.captured", "lead.marked_spam"])
        marked = OutboxEvent.all_tenants.get(type="lead.marked_spam")
        self.assertEqual(marked.data["by"], "system")
        self.assertIn("links_in_message", marked.data["signals"])
        self.assertFalse(OutboundMessage.all_tenants.exists())  # no notice for spam


class RateLimitTests(LeadBase):
    def test_the_shipped_numbers(self):
        self.assertEqual(
            [(r.limit, r.window) for r in lead_limits.CREATE_IP],
            [(6, timedelta(minutes=10)), (30, timedelta(hours=24))],
        )
        self.assertEqual(
            [(r.limit, r.window) for r in lead_limits.CREATE_EMAIL], [(10, timedelta(hours=24))]
        )
        self.assertEqual(
            [(r.limit, r.window) for r in lead_limits.SPAM_TRAP_IP], [(3, timedelta(hours=1))]
        )

    def test_the_seventh_in_ten_minutes_from_one_ip_is_refused(self):
        for _ in range(6):
            self.create()
        with self.assertRaises(RateLimited) as cm:
            self.create()
        self.assertEqual(cm.exception.rule, "lead.create.ip.10m")
        self.assertEqual(Lead.all_tenants.count(), 6)

    def test_a_flood_writes_one_blocked_row_per_window_and_creates_nothing(self):
        for _ in range(6):
            self.create()
        events = OutboxEvent.all_tenants.count()
        for _ in range(4):
            with self.assertRaises(RateLimited):
                self.create()
        blocked = log(outcome="blocked")
        self.assertEqual(blocked.count(), 1)
        self.assertEqual(blocked.get().problem, {"rate_limited": "lead.create.ip.10m"})
        self.assertEqual(OutboxEvent.all_tenants.count(), events)
        self.assertEqual(Consent.all_tenants.count(), 6)

    def test_the_email_limit_spans_listings_and_ips(self):
        for i in range(10):
            self.create(
                ip=f"198.51.100.{i + 1}",
                listing=self.make_listing(f"biz-{i}"),
                contact={"name": "Priya", "email": "same@example.test", "phone_e164": ""},
            )
        with self.assertRaises(RateLimited) as cm:
            self.create(
                ip="198.51.100.99",
                contact={"name": "Priya", "email": "same@example.test", "phone_e164": ""},
            )
        self.assertEqual(cm.exception.rule, "lead.create.email.24h")

    def test_no_ip_means_no_ip_limit(self):
        for _ in range(8):
            self.create(ip=None)
        self.assertEqual(Lead.all_tenants.count(), 8)

    def test_tenants_have_their_own_budget(self):
        other = Tenant.objects.create(
            slug="other", name="Other", primary_domain="other.test",
            settings={"leads": {"enabled": True}},
        )
        lt = ListingType.all_tenants.create(
            tenant=other, key="business", label_singular="B", label_plural="Bs",
            path_segment="businesses",
        )
        theirs = Listing.all_tenants.create(
            tenant=other, listing_type=lt, slug="theirs", name="Theirs",
            visibility=Listing.Visibility.PUBLISHED,
        )
        for _ in range(6):
            self.create()
        with tenant_context(other):
            lead = leads.create_lead(
                other, listing=theirs, kind="contact_form",
                contact={"name": "Priya", "email": "p@example.test", "phone_e164": ""},
                message=GOOD_MESSAGE, consent=GRANTED, source_page="", ip=IP,
            )
        self.assertEqual(lead.tenant_id, other.pk)

    def test_the_trap_counts_then_refuses_and_creates_nothing(self):
        for _ in range(3):
            leads.reject_spam_trap(self.tenant, listing=self.listing, ip=IP)
        with self.assertRaises(RateLimited) as cm:
            leads.reject_spam_trap(self.tenant, listing=self.listing, ip=IP)
        self.assertEqual(cm.exception.rule, "lead.spam_trap.ip.1h")
        self.assertFalse(Lead.all_tenants.exists())
        self.assertEqual(log(outcome="blocked").count(), 1)

    def test_the_trap_without_an_ip_does_not_count(self):
        for _ in range(5):
            leads.reject_spam_trap(self.tenant, listing=self.listing, ip=None)


class MarkSpamTests(LeadBase):
    def mark(self, lead, operator):
        with tenant_context(self.tenant):
            return leads.mark_lead_spam(self.tenant, lead=lead, operator=operator)

    def test_a_moderator_marks_a_lead_and_the_event_says_who(self):
        lead = self.create()
        moderator = self.op(Role.MODERATOR)
        marked = self.mark(lead, moderator)
        self.assertTrue(marked.marked_spam)
        self.assertEqual(self.fresh(lead).spam_marked_by_id, moderator.pk)
        event = OutboxEvent.all_tenants.get(type="lead.marked_spam")
        self.assertEqual(event.data, {"lead_id": lead.public_id, "by": moderator.public_id, "signals": ["moderator"]})
        self.assertEqual(event.actor["id"], moderator.public_id)
        row = log("lead.mark_spam").get()
        self.assertEqual((row.outcome, row.payload), ("applied", {"lead_id": lead.public_id}))

    def test_support_is_blocked_and_the_log_says_so(self):
        lead = self.create()
        with self.assertRaises(leads.LeadRefused) as cm:
            self.mark(lead, self.op(Role.SUPPORT))
        self.assertEqual(cm.exception.reason, "forbidden")
        self.assertFalse(self.fresh(lead).marked_spam)
        self.assertEqual(log("lead.mark_spam").get().outcome, "blocked")

    def test_a_non_member_is_blocked(self):
        lead = self.create()
        stranger = self.op(Role.ADMIN, tenant=Tenant.objects.create(slug="other", name="Other"))
        with self.assertRaises(leads.LeadRefused):
            self.mark(lead, stranger)

    def test_marking_twice_is_rejected_without_a_second_event(self):
        lead = self.create()
        moderator = self.op(Role.MODERATOR)
        self.mark(lead, moderator)
        with self.assertRaises(leads.LeadRefused) as cm:
            self.mark(lead, moderator)
        self.assertEqual(cm.exception.reason, "already_marked")
        self.assertEqual(OutboxEvent.all_tenants.filter(type="lead.marked_spam").count(), 1)
        self.assertEqual(log("lead.mark_spam", outcome="rejected").count(), 1)
