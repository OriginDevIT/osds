"""Claims PR 4b (#218): the operator notice for every claim entering manual
review (decisions.md §4.6; spec §9.6 "with the operator notified").

TransactionTestCase throughout, like the rest of the claim tests.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from audit.models import OutboundMessage
from directory import claim_review
from directory.models import Claim, Listing
from directory.tests.test_claim_review import (
    DANA,
    LISTING_EMAIL,
    OTHER,
    Role,
    _any_wrong_code,
    _Base,
    _capture_code,
)
from osds.tenancy import tenant_context
from tenants.models import Operator, StaffMembership, Tenant

KIND = claim_review.OPERATOR_NOTICE_KIND


class _NoticeBase(_Base):
    def notices(self, address=None):
        qs = OutboundMessage.all_tenants.filter(tenant=self.tenant, kind=KIND).order_by("id")
        return qs.filter(to_address=address) if address else qs

    def address(self, operator):
        return operator.email

    def age_notices(self, minutes=16):
        OutboundMessage.all_tenants.filter(kind=KIND).update(
            created_at=timezone.now() - timedelta(minutes=minutes)
        )

    def other_claimant(self, n):
        return {**OTHER, "email": f"person{n}@elsewhere.example", "name": f"Person {n}"}


class RecipientTests(_NoticeBase):
    def test_active_memberships_at_editor_and_above_are_told_and_nobody_else(self):
        editor, manager, admin = (
            self.op(Role.EDITOR), self.op(Role.MANAGER), self.op(Role.ADMIN),
        )
        moderator, support = self.op(Role.MODERATOR), self.op(Role.SUPPORT)
        self.submit()
        told = set(self.notices().values_list("to_address", flat=True))
        self.assertEqual(told, {editor.email, manager.email, admin.email})
        self.assertNotIn(moderator.email, told)
        self.assertNotIn(support.email, told)

    def test_pending_memberships_inactive_operators_and_other_tenants_are_not_told(self):
        pending = self.op(Role.ADMIN)
        StaffMembership.objects.filter(operator=pending).update(
            status=StaffMembership.Status.PENDING
        )
        inactive = self.op(Role.ADMIN)
        Operator.objects.filter(pk=inactive.pk).update(is_active=False)
        other = Tenant.objects.create(slug="other", name="Other")
        elsewhere = self.op(Role.ADMIN, tenant=other)
        editor = self.op(Role.EDITOR)
        self.submit()
        self.assertEqual(list(self.notices().values_list("to_address", flat=True)), [editor.email])
        self.assertFalse(
            OutboundMessage.all_tenants.filter(to_address=elsewhere.email).exists()
        )

    def test_each_recipient_is_told_once(self):
        editor = self.op(Role.EDITOR)
        self.submit()
        self.assertEqual(self.notices(editor.email).count(), 1)

    def test_no_recipients_is_not_an_error(self):
        self.submit()
        self.assertFalse(self.notices().exists())


class MessageTests(_NoticeBase):
    def setUp(self):
        super().setUp()
        self.editor = self.op(Role.EDITOR)

    def test_subject_body_deadline_and_link(self):
        before = timezone.now()
        self.submit()
        message = self.notices().get()
        self.assertEqual(message.subject, "Claims waiting for review on Acme")
        self.assertIn("1 claim is waiting for review on Acme.", message.body_text)
        self.assertIn("https://acme.test/admin/claims/", message.body_text)
        self.assertAlmostEqual(
            (message.expires_at - before).total_seconds(),
            timedelta(hours=48).total_seconds(), delta=30,
        )
        self.assertEqual(message.status, OutboundMessage.Status.PENDING)

    def test_no_link_without_an_absolute_base(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(domain_verified_at=None)
        self.tenant.refresh_from_db()
        self.submit()
        body = self.notices().get().body_text
        self.assertNotIn("/admin/claims/", body)
        self.assertNotIn("http", body)
        self.assertIn("1 claim is waiting", body)

    def test_nothing_the_claimant_wrote_reaches_the_message(self):
        claimant = {
            "name": "Zed Mallory-Unique", "email": "zed.unique@elsewhere.example",
            "phone_e164": "+17735550199", "role_claimed": "owner-of-the-universe",
        }
        self.submit(claimant=claimant)
        message = self.notices().get()
        text = f"{message.subject}\n{message.body_text}"
        for value in ("Zed", "Mallory", "unique", "7735550199", "universe"):
            self.assertNotIn(value.lower(), text.lower())

    def test_the_message_never_names_the_listing_or_its_contact(self):
        self.submit()
        text = self.notices().get().body_text
        self.assertNotIn(self.listing.name, text)
        self.assertNotIn(LISTING_EMAIL, text)


class EntryPointTests(_NoticeBase):
    def setUp(self):
        super().setUp()
        self.editor = self.op(Role.EDITOR)

    def test_a_claim_that_chose_manual_review(self):
        self.submit(method="manual")
        self.assertEqual(self.notices().count(), 1)

    def test_a_domain_email_claim_flipped_at_submit_for_any_reason(self):
        # address not at the website's host
        self.submit(method="domain_email", claimant=self.other_claimant(1))
        self.assertEqual(self.notices().count(), 1)
        self.age_notices()
        # no mail available
        self.mail._available = False
        self.submit(method="domain_email", claimant={**DANA, "email": "dana@hoffmanplumbing.example"})
        self.assertEqual(self.notices().count(), 2)

    def test_a_clean_domain_email_claim_is_not_in_review(self):
        self.submit(method="domain_email")
        self.assertFalse(self.notices().exists())

    def test_a_flip_on_resend(self):
        claim = self.submit(method="domain_email")
        self.assertFalse(self.notices().exists())
        self.listing.website = ""
        self.listing.save(update_fields=["website"])
        with tenant_context(self.tenant):
            from directory import services

            services.start_claim_verification(self.tenant, claim=claim)
        self.assertEqual(self.fresh(claim).method, "manual")
        self.assertEqual(self.notices().count(), 1)

    def test_a_flip_at_the_verification_lock(self):
        claim = self.submit(method="domain_email")
        guard = self.guard(claim)
        guard.cooldowns = 2
        guard.save(update_fields=["cooldowns"])
        wrong = _any_wrong_code(claim)
        for _ in range(4):
            claim, _ = self.verify(claim, wrong)
        self.assertFalse(self.notices().exists())  # still in verification
        self.verify(claim, wrong)  # the fifth wrong entry opens the third cooldown
        self.assertEqual(self.fresh(claim).method, "manual")
        self.assertEqual(self.notices().count(), 1)

    def test_a_dispute_at_submit_sends_exactly_one(self):
        self.own()  # also adds an editor of its own, so count per recipient
        self.age_notices()
        before = self.notices(self.editor.email).count()
        self.submit(claimant=DANA)
        self.assertEqual(self.notices(self.editor.email).count(), before + 1)

    def test_a_dispute_that_is_also_flipped_at_submit_still_sends_one(self):
        self.own()
        self.age_notices()
        before = self.notices(self.editor.email).count()
        self.submit(method="domain_email", claimant=self.other_claimant(2))
        self.assertEqual(self.notices(self.editor.email).count(), before + 1)

    def test_a_dispute_from_a_verify_race(self):
        claim = self.submit(method="domain_email")
        code = _capture_code(self, claim)
        self.assertFalse(self.notices().exists())
        self.claim_behind_their_back()
        self.verify(claim, code)
        self.assertEqual(self.fresh(claim).status, Claim.Status.DISPUTED)
        self.assertEqual(self.notices().count(), 1)

    def test_a_disputed_claim_that_later_locks_is_not_announced_twice(self):
        self.own()
        claim = self.submit(method="domain_email")
        self.assertEqual(self.fresh(claim).status, Claim.Status.DISPUTED)
        self.age_notices()
        before = self.notices().count()
        guard = self.guard(claim)
        guard.cooldowns = 2
        guard.save(update_fields=["cooldowns"])
        wrong = _any_wrong_code(claim)
        for _ in range(5):
            claim, _ = self.verify(claim, wrong)
        self.assertEqual(self.notices().count(), before)

    def test_decisions_do_not_notify(self):
        claim = self.submit()
        self.age_notices()
        before = self.notices().count()
        self.approve(claim, self.editor)
        self.assertEqual(self.notices().count(), before)
        loser_claim = self.submit(claimant=self.other_claimant(3))  # a dispute
        self.age_notices()
        before = self.notices().count()
        self.reject(loser_claim, self.editor)
        self.assertEqual(self.notices().count(), before)


class ThrottleTests(_NoticeBase):
    def setUp(self):
        super().setUp()
        self.editor = self.op(Role.EDITOR)

    def test_at_most_one_message_per_recipient_in_the_window(self):
        for n in range(4):
            self.submit(claimant=self.other_claimant(n))
        self.assertEqual(self.notices(self.editor.email).count(), 1)
        self.assertIn("1 claim is waiting", self.notices().get().body_text)

    def test_after_the_window_the_next_message_states_the_whole_backlog(self):
        for n in range(3):
            self.submit(claimant=self.other_claimant(n))
        self.age_notices(minutes=16)
        self.submit(claimant=self.other_claimant(9))
        second = self.notices(self.editor.email).order_by("id").last()
        self.assertEqual(self.notices(self.editor.email).count(), 2)
        self.assertIn("4 claims are waiting", second.body_text)

    def test_just_inside_the_window_still_throttles(self):
        self.submit(claimant=self.other_claimant(1))
        self.age_notices(minutes=14)
        self.submit(claimant=self.other_claimant(2))
        self.assertEqual(self.notices(self.editor.email).count(), 1)

    def test_the_throttle_is_per_recipient(self):
        self.submit(claimant=self.other_claimant(1))
        newcomer = self.op(Role.ADMIN)
        self.submit(claimant=self.other_claimant(2))
        self.assertEqual(self.notices(self.editor.email).count(), 1)
        self.assertEqual(self.notices(newcomer.email).count(), 1)
        self.assertIn("2 claims are waiting", self.notices(newcomer.email).get().body_text)

    def test_the_throttle_is_derived_from_outbound_messages_not_a_table(self):
        self.submit(claimant=self.other_claimant(1))
        self.assertEqual(self.notices().count(), 1)
        # Forget the message and the throttle forgets with it.
        OutboundMessage.all_tenants.filter(kind=KIND).delete()
        self.submit(claimant=self.other_claimant(2))
        self.assertEqual(self.notices().count(), 1)

    def test_an_expired_or_dead_message_still_counts_as_the_leading_edge(self):
        self.submit(claimant=self.other_claimant(1))
        OutboundMessage.all_tenants.filter(kind=KIND).update(
            status=OutboundMessage.Status.EXPIRED
        )
        self.submit(claimant=self.other_claimant(2))
        self.assertEqual(self.notices().count(), 1)

    def test_other_kinds_of_mail_do_not_throttle_the_notice(self):
        # the claimant-facing mail and the anti-hijack notice are other kinds
        OutboundMessage.all_tenants.create(
            tenant=self.tenant, kind="claim.rejected", to_address=self.editor.email,
            subject="x", body_text="x", expires_at=timezone.now() + timedelta(days=1),
        )
        self.submit()
        self.assertEqual(self.notices(self.editor.email).count(), 1)

    def test_the_count_is_claims_waiting_not_queue_items(self):
        second = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="second", name="Second Co",
            visibility=Listing.Visibility.PUBLISHED,
        )
        self.own()  # a sitting owner on the first listing
        self.submit(listing=second)  # a manual claim in review: 1
        self.age_notices()
        self.submit(claimant=DANA)  # a dispute on the owned listing: 2
        with tenant_context(self.tenant):
            self.assertEqual(claim_review.waiting_claims_count(), 2)
        newest = self.notices().order_by("id").last()
        self.assertIn("2 claims are waiting", newest.body_text)

    def test_the_notice_commits_with_the_claim(self):
        self.submit()
        message = self.notices().get()
        claim = Claim.all_tenants.get(tenant=self.tenant)
        self.assertEqual(claim.review_reason, "chosen")
        self.assertLessEqual(message.created_at, timezone.now())
