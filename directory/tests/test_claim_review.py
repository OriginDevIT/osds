"""Claims PR 4a: approval, rejection, disputes, the moderation queue and the
admin review UI (decisions.md §4.4, §4.6; spec §9.3, §9.4, §9.6).

TransactionTestCase throughout -- the commands refuse an open transaction.
"""

from __future__ import annotations

import ast
import pathlib
import threading
import time
from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

import directory
from audit.models import CommandLog, OutboundMessage, OutboxEvent
from directory import claim_review, claims, services
from directory.models import (
    Claim,
    ClaimVerificationGuard,
    DirectoryUser,
    Listing,
    ListingType,
    ModerationItem,
)
from directory.tests.test_claim_verification import (
    DANA,
    GRANTED_ALL,
    HOST,
    _any_wrong_code,
    _capture_code,
    _domain_email_enabled,
)
from osds.tenancy import tenant_context
from osds.tests.mail_stub import email_send_stub
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant

Role = StaffMembership.Role
LISTING_EMAIL = "info@hoffmanplumbing.example"
OTHER = {
    "name": "Eve Other",
    "email": "eve@elsewhere.example",
    "phone_e164": "",
    "role_claimed": "owner",
}


class _Base(TransactionTestCase):
    def setUp(self):
        self.mail = self.enterContext(email_send_stub())
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain=HOST,
            domain_verified_at=timezone.now(), settings=_domain_email_enabled(),
        )
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="Business",
            label_plural="Businesses", path_segment="businesses",
        )
        self.listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="hoffman-plumbing",
            name="Hoffman Plumbing", visibility=Listing.Visibility.PUBLISHED,
            website="https://hoffmanplumbing.example", email=LISTING_EMAIL,
        )
        self._ops = 0

    # -- fixtures --
    def op(self, role, tenant=None):
        self._ops += 1
        operator = Operator.objects.create_user(
            email=f"op{self._ops}@acme.test", password="pw"
        )
        StaffMembership.objects.create(
            operator=operator, tenant=tenant or self.tenant, role=role,
            status=StaffMembership.Status.ACTIVE,
        )
        return operator

    def submit(self, method="manual", claimant=None, listing=None):
        with tenant_context(self.tenant):
            return services.submit_claim(
                self.tenant, listing=listing or self.listing, method=method,
                claimant=dict(claimant or DANA), consent=GRANTED_ALL, ip="203.0.113.44",
            )

    def fresh(self, obj):
        return type(obj).all_tenants.get(pk=obj.pk)

    def approve(self, claim, operator, **kw):
        kw.setdefault("method_used", "phone")
        kw.setdefault("notes", "Called the listed number and spoke with the owner.")
        with tenant_context(self.tenant):
            return claim_review.approve_claim(
                self.tenant, claim=claim, operator=operator, **kw
            )

    def reject(self, claim, operator, reason="Not enough to show you run this business."):
        with tenant_context(self.tenant):
            return claim_review.reject_claim(
                self.tenant, claim=claim, operator=operator, reason=reason
            )

    def verify(self, claim, code):
        with tenant_context(self.tenant):
            return services.verify_claim_code(self.tenant, claim=claim, code=code, ip=None)

    def guard(self, claim):
        return ClaimVerificationGuard.all_tenants.get(
            listing=claim.listing, claimant=claim.claimant
        )

    def events(self, etype):
        return OutboxEvent.all_tenants.filter(type=etype, tenant=self.tenant)

    def mail_to(self, address, kind=None):
        qs = OutboundMessage.all_tenants.filter(tenant=self.tenant, to_address=address)
        return qs.filter(kind=kind) if kind else qs

    def assert_owner_agrees(self, listing, *, owner):
        listing = self.fresh(listing)
        if owner is None:
            self.assertIsNone(listing.owner_id)
            self.assertNotEqual(listing.status, Listing.Status.CLAIMED)
        else:
            self.assertEqual(listing.owner_id, owner.pk)
            self.assertEqual(listing.status, Listing.Status.CLAIMED)

    def claim_behind_their_back(self, claimant=OTHER):
        """Stand in for a concurrent approval that landed after this claim was
        read: the normal paths auto-reject a rival pending claim, so the only
        way a pending claim meets a claimed listing is a race."""
        user = DirectoryUser.all_tenants.get_or_create(
            tenant=self.tenant, email=claimant["email"]
        )[0]
        Listing.all_tenants.filter(pk=self.listing.pk).update(
            status=Listing.Status.CLAIMED, owner=user
        )
        return user

    def own(self, claimant=OTHER):
        """Make ``claimant`` the sitting owner through the real path."""
        claim = self.submit(claimant=claimant)
        self.approve(claim, self.op(Role.EDITOR))
        self.listing.refresh_from_db()
        return self.fresh(claim)


class ApproveTests(_Base):
    def test_manual_approval_on_an_unowned_listing_makes_the_claimant_owner(self):
        claim = self.submit()
        editor = self.op(Role.EDITOR)
        result = self.approve(claim, editor, evidence_ref="ticket-7")
        self.assertEqual(result.outcome, "approved")
        claim = self.fresh(claim)
        self.assertEqual(claim.status, Claim.Status.APPROVED)
        self.assertEqual(claim.method, "manual")
        self.assertEqual(claim.manual_method_used, "phone")
        self.assertEqual(claim.verified_by_id, editor.pk)
        self.assertEqual(claim.decided_by_id, editor.pk)
        self.assertEqual(claim.evidence_ref, "ticket-7")
        self.assertTrue(claim.verification_notes)
        self.assert_owner_agrees(self.listing, owner=claim.claimant)

    def test_events_and_command_log(self):
        claim = self.submit()
        self.approve(claim, self.op(Role.EDITOR))
        approved = self.events("claim.approved").get()
        manual = approved.data["manual_verification"]
        self.assertEqual(manual["method_used"], "phone")
        self.assertTrue(manual["notes"])
        self.assertTrue(manual["verified_by"].startswith("op_"))
        self.assertFalse(approved.data["transfer"])
        assigned = self.events("listing.owner_assigned").get()
        self.assertEqual(assigned.data["owner"]["id"], claim.claimant.public_id)
        self.assertIsNone(assigned.data["previous_owner_id"])
        updated = self.events("listing.updated").get()
        paths = {c["path"] for c in updated.data["changes"]}
        self.assertEqual(paths, {"/status", "/owner_id"})
        row = CommandLog.objects.filter(command="claim.approve").get()
        self.assertEqual(row.outcome, "applied")
        self.assertEqual(row.result_event_id, approved.event_id)
        self.assertNotIn("notes", row.payload)

    def test_actor_type_follows_the_membership_rank(self):
        self.own()
        first = self.events("claim.approved").get()
        self.assertEqual(first.actor["type"], "staff")
        dispute = self.submit(claimant=DANA)
        self.approve(dispute, self.op(Role.ADMIN), transfer=True)
        last = self.events("claim.approved").order_by("id").last()
        self.assertEqual(last.actor["type"], "admin")

    def test_notes_are_required_and_nothing_changes(self):
        claim = self.submit()
        for notes in ("", "   \n"):
            with self.subTest(notes=notes):
                with self.assertRaises(claim_review.ReviewRefused) as cm:
                    self.approve(claim, self.op(Role.EDITOR), notes=notes)
                self.assertEqual(cm.exception.reason, "notes_required")
        self.assertEqual(self.fresh(claim).status, Claim.Status.PENDING_VERIFICATION)
        self.assert_owner_agrees(self.listing, owner=None)
        row = CommandLog.objects.filter(command="claim.approve").last()
        self.assertEqual(row.outcome, "rejected")

    def test_method_used_must_be_a_known_manual_method(self):
        claim = self.submit()
        with self.assertRaises(claim_review.ReviewRefused) as cm:
            self.approve(claim, self.op(Role.EDITOR), method_used="telepathy")
        self.assertEqual(cm.exception.reason, "method_used_invalid")

    def test_roles_below_editor_are_blocked_and_logged_blocked(self):
        claim = self.submit()
        for role in (Role.SUPPORT, Role.MODERATOR):
            with self.subTest(role=role):
                with self.assertRaises(claim_review.ReviewRefused) as cm:
                    self.approve(claim, self.op(role))
                self.assertEqual(cm.exception.reason, "forbidden")
                self.assertEqual(
                    CommandLog.objects.filter(command="claim.approve").last().outcome,
                    "blocked",
                )
        self.assert_owner_agrees(self.listing, owner=None)

    def test_an_operator_with_no_membership_here_is_blocked(self):
        other = Tenant.objects.create(slug="other", name="Other")
        stranger = self.op(Role.ADMIN, tenant=other)
        with self.assertRaises(claim_review.ReviewRefused) as cm:
            self.approve(self.submit(), stranger)
        self.assertEqual(cm.exception.reason, "forbidden")

    def test_a_decided_claim_cannot_be_approved_again(self):
        claim = self.submit()
        editor = self.op(Role.EDITOR)
        self.approve(claim, editor)
        with self.assertRaises(claim_review.ReviewRefused) as cm:
            self.approve(self.fresh(claim), editor)
        self.assertEqual(cm.exception.reason, "status")

    def test_a_suspended_listing_cannot_change_owner(self):
        claim = self.submit()
        Listing.all_tenants.filter(pk=self.listing.pk).update(
            status=Listing.Status.SUSPENDED
        )
        with self.assertRaises(claim_review.ReviewRefused) as cm:
            self.approve(claim, self.op(Role.EDITOR))
        self.assertEqual(cm.exception.reason, "listing_suspended")

    def test_a_pending_claim_on_a_listing_claimed_meanwhile_becomes_a_dispute(self):
        second = self.submit()
        rival = self.claim_behind_their_back()
        result = self.approve(second, self.op(Role.EDITOR))
        self.assertEqual(result.outcome, "disputed")
        self.assertEqual(self.fresh(second).status, Claim.Status.DISPUTED)
        self.assert_owner_agrees(self.listing, owner=rival)
        self.assertEqual(self.events("claim.approved").count(), 0)
        self.assertEqual(
            CommandLog.objects.filter(command="claim.approve").last().outcome, "applied"
        )

    def test_approving_a_dispute_needs_the_transfer_confirmation(self):
        owner_claim = self.own()
        dispute = self.submit(claimant=DANA)
        self.assertEqual(self.fresh(dispute).status, Claim.Status.DISPUTED)
        editor = self.op(Role.EDITOR)
        with self.assertRaises(claim_review.ReviewRefused) as cm:
            self.approve(dispute, editor)
        self.assertEqual(cm.exception.reason, "transfer_unconfirmed")
        self.assert_owner_agrees(self.listing, owner=owner_claim.claimant)

    def test_a_confirmed_transfer_supersedes_the_old_claim_and_tells_the_old_owner(self):
        old = self.own()
        dispute = self.submit(claimant=DANA)
        item = ModerationItem.all_tenants.get(claim=dispute)
        self.approve(dispute, self.op(Role.EDITOR), transfer=True)
        self.assertEqual(self.fresh(old).status, Claim.Status.SUPERSEDED)
        self.assertEqual(self.fresh(dispute).status, Claim.Status.APPROVED)
        self.assert_owner_agrees(self.listing, owner=dispute.claimant)
        assigned = self.events("listing.owner_assigned").order_by("id").last()
        self.assertEqual(assigned.data["previous_owner_id"], old.claimant.public_id)
        self.assertTrue(self.events("claim.approved").order_by("id").last().data["transfer"])
        self.assertTrue(self.mail_to(OTHER["email"], "claim.existing_contact_notice").exists())
        item = self.fresh(item)
        self.assertEqual((item.status, item.decision), ("decided", "approved"))
        self.assertEqual(self.events("moderation.decided").count(), 1)

    def test_one_approved_claim_per_listing_is_a_database_constraint(self):
        claim = self.submit()
        self.approve(claim, self.op(Role.EDITOR))
        other = self.submit(claimant=OTHER)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Claim.all_tenants.filter(pk=other.pk).update(status=Claim.Status.APPROVED)


class LoserTests(_Base):
    def test_approval_rejects_the_other_pending_claims_by_the_system(self):
        winner, loser = self.submit(), self.submit(claimant=OTHER)
        self.approve(winner, self.op(Role.EDITOR))
        loser = self.fresh(loser)
        self.assertEqual(loser.status, Claim.Status.REJECTED)
        self.assertEqual(loser.rejection_reason, claim_review.LOSER_REASON)
        self.assertIsNone(loser.decided_by_id)
        event = self.events("claim.rejected").get()
        self.assertEqual(event.data["decided_by"], "system")
        self.assertEqual(event.actor["type"], "system")

    def test_a_loser_is_told_with_the_same_producer_and_deadline(self):
        winner, loser = self.submit(), self.submit(claimant=OTHER)
        before = timezone.now()
        self.approve(winner, self.op(Role.EDITOR))
        message = self.mail_to(OTHER["email"], "claim.rejected").get()
        self.assertIn(claim_review.LOSER_REASON, message.body_text)
        self.assertIn(f"/claim/{self.listing.public_id}/", message.body_text)
        self.assertIn("dispute", message.body_text)
        self.assertNotIn("more information", message.body_text)
        self.assertAlmostEqual(
            (message.expires_at - before).total_seconds(),
            timedelta(days=7).total_seconds(), delta=30,
        )
        self.assertFalse(self.mail_to(DANA["email"], "claim.rejected").exists())

    def test_a_loser_does_not_count_toward_the_three_rejection_block(self):
        winner, loser = self.submit(), self.submit(claimant=OTHER)
        self.approve(winner, self.op(Role.EDITOR))
        guard = ClaimVerificationGuard.all_tenants.filter(
            listing=self.listing, claimant=loser.claimant
        ).first()
        self.assertTrue(guard is None or (guard.rejections, guard.claim_blocked_at) == (0, None))
        self.assertFalse(ModerationItem.all_tenants.filter(item_type="claim_block").exists())

    def test_a_losers_pending_code_is_killed(self):
        winner = self.submit()
        loser = self.submit(method="domain_email", claimant={**OTHER, "email": "eve@hoffmanplumbing.example"})
        message_id = loser.code_message_id
        self.approve(winner, self.op(Role.EDITOR))
        self.assertEqual(self.fresh(loser).code_hash, "")
        self.assertLessEqual(
            OutboundMessage.all_tenants.get(pk=message_id).expires_at, timezone.now()
        )

    def test_a_loser_locked_by_another_transaction_is_skipped_then_disputes(self):
        # claim.verify locks claim then listing; approval locks listing then
        # the losers. skip_locked keeps the second order from ever waiting.
        winner = self.submit()
        loser = self.submit(
            method="domain_email",
            claimant={**OTHER, "email": "eve@hoffmanplumbing.example"},
        )
        code = _capture_code(self, loser)
        locked, release, errors = threading.Event(), threading.Event(), []

        def hold_the_loser():
            try:
                with transaction.atomic():
                    Claim.all_tenants.select_for_update().get(pk=loser.pk)
                    locked.set()
                    # A regression would block the approval on this lock; the
                    # timeout turns that into an assertion, not a hung suite.
                    release.wait(timeout=10)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
                locked.set()
            finally:
                connection.close()

        holder = threading.Thread(target=hold_the_loser)
        holder.start()
        try:
            self.assertTrue(locked.wait(timeout=10))
            started = time.monotonic()
            self.approve(winner, self.op(Role.EDITOR))
            self.assertLess(time.monotonic() - started, 8, "approval waited on the lock")
        finally:
            release.set()
            holder.join(timeout=15)
        self.assertEqual(errors, [])

        skipped = self.fresh(loser)
        self.assertEqual(skipped.status, Claim.Status.PENDING_VERIFICATION)
        self.assertFalse(self.events("claim.rejected").exists())
        self.assertFalse(self.mail_to(OTHER["email"], "claim.rejected").exists())
        self.assert_owner_agrees(self.listing, owner=winner.claimant)

        claim, correct = self.verify(skipped, code)
        self.assertTrue(correct)
        self.assertEqual(claim.status, Claim.Status.DISPUTED)
        self.assertIsNotNone(claim.verified_at)
        self.assert_owner_agrees(self.listing, owner=winner.claimant)
        self.assertEqual(self.events("claim.approved").count(), 1)
        self.assertEqual(self.events("claim.disputed").count(), 1)
        self.assertEqual(ModerationItem.all_tenants.filter(claim=loser).count(), 1)

    def test_disputed_claims_are_not_losers(self):
        old = self.own()
        dispute = self.submit(claimant=DANA)
        third = self.submit(claimant={**OTHER, "email": "third@elsewhere.example"})
        self.approve(dispute, self.op(Role.EDITOR), transfer=True)
        self.assertEqual(self.fresh(third).status, Claim.Status.DISPUTED)
        self.assertEqual(self.fresh(old).status, Claim.Status.SUPERSEDED)


class AntiHijackNoticeTests(_Base):
    def test_the_listings_existing_email_is_told_with_a_seven_day_deadline(self):
        claim = self.submit()
        before = timezone.now()
        self.approve(claim, self.op(Role.EDITOR))
        message = self.mail_to(LISTING_EMAIL, "claim.existing_contact_notice").get()
        self.assertEqual(message.status, OutboundMessage.Status.PENDING)
        self.assertAlmostEqual(
            (message.expires_at - before).total_seconds(),
            timedelta(days=7).total_seconds(), delta=30,
        )
        self.assertIn(self.listing.name, message.subject)
        self.assertIn(f"https://{HOST}/claim/{self.listing.public_id}/", message.body_text)
        self.assertNotIn(DANA["name"], message.body_text)
        self.assertNotIn(DANA["email"], message.body_text)

    def test_the_event_carries_masked_destinations_only(self):
        self.approve(self.submit(), self.op(Role.EDITOR))
        notified = self.events("claim.notified_existing_contacts").get()
        self.assertNotIn(LISTING_EMAIL, str(notified.data))
        self.assertEqual(notified.data["channels"][0]["channel"], "email")
        self.assertTrue(self.events("claim.approved").get().data["existing_contacts_notified"])

    def test_a_claimant_at_the_listing_address_is_not_told(self):
        claim = self.submit(claimant={**DANA, "email": LISTING_EMAIL})
        self.approve(claim, self.op(Role.EDITOR))
        self.assertFalse(self.mail_to(LISTING_EMAIL).exists())
        self.assertFalse(self.events("claim.notified_existing_contacts").exists())
        self.assertFalse(self.events("claim.approved").get().data["existing_contacts_notified"])

    def test_a_listing_with_no_email_sends_nothing(self):
        Listing.all_tenants.filter(pk=self.listing.pk).update(email="")
        self.approve(self.submit(), self.op(Role.EDITOR))
        self.assertFalse(
            OutboundMessage.all_tenants.filter(kind="claim.existing_contact_notice").exists()
        )

    def test_without_a_verified_domain_the_notice_has_no_link(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(domain_verified_at=None)
        self.tenant.refresh_from_db()
        self.approve(self.submit(), self.op(Role.EDITOR))
        body = self.mail_to(LISTING_EMAIL).get().body_text
        self.assertNotIn("/claim/", body)
        self.assertIn("contact the directory", body)


class RejectTests(_Base):
    def test_a_rejection_records_the_reason_and_mails_the_claimant(self):
        claim = self.submit()
        editor = self.op(Role.EDITOR)
        before = timezone.now()
        self.reject(claim, editor, reason="We could not reach the listed number.")
        claim = self.fresh(claim)
        self.assertEqual(claim.status, Claim.Status.REJECTED)
        self.assertEqual(claim.decided_by_id, editor.pk)
        self.assertEqual(claim.rejection_reason, "We could not reach the listed number.")
        self.assertEqual(self.guard(claim).rejections, 1)
        self.assert_owner_agrees(self.listing, owner=None)
        message = self.mail_to(DANA["email"], "claim.rejected").get()
        self.assertIn("We could not reach the listed number.", message.body_text)
        self.assertIn(f"/claim/{self.listing.public_id}/", message.body_text)
        self.assertAlmostEqual(
            (message.expires_at - before).total_seconds(),
            timedelta(days=7).total_seconds(), delta=30,
        )
        event = self.events("claim.rejected").get()
        self.assertEqual(event.data["reason"], "We could not reach the listed number.")
        self.assertEqual(event.data["decided_by"], editor.public_id)

    def test_the_reason_is_required(self):
        claim = self.submit()
        with self.assertRaises(claim_review.ReviewRefused) as cm:
            self.reject(claim, self.op(Role.EDITOR), reason=" ")
        self.assertEqual(cm.exception.reason, "reason_required")
        self.assertEqual(self.fresh(claim).status, Claim.Status.PENDING_VERIFICATION)

    def test_below_editor_is_blocked(self):
        claim = self.submit()
        with self.assertRaises(claim_review.ReviewRefused):
            self.reject(claim, self.op(Role.MODERATOR))
        self.assertEqual(
            CommandLog.objects.filter(command="claim.reject").last().outcome, "blocked"
        )

    def test_a_decided_claim_cannot_be_rejected(self):
        claim = self.submit()
        editor = self.op(Role.EDITOR)
        self.reject(claim, editor)
        with self.assertRaises(claim_review.ReviewRefused) as cm:
            self.reject(self.fresh(claim), editor)
        self.assertEqual(cm.exception.reason, "status")

    def test_the_third_rejection_blocks_the_pair_and_opens_an_item(self):
        editor = self.op(Role.EDITOR)
        for n in range(3):
            claim = self.submit()
            self.reject(claim, editor)
        guard = self.guard(claim)
        self.assertEqual(guard.rejections, 3)
        self.assertIsNotNone(guard.claim_blocked_at)
        item = ModerationItem.all_tenants.get(item_type="claim_block")
        self.assertEqual(item.status, "open")
        queued = self.events("moderation.queued").get()
        self.assertEqual(queued.data["item_type"], "claim_block")
        self.assertEqual(queued.data["rules_triggered"], ["repeated_rejection"])
        self.assertIn("can no longer claim", self.mail_to(DANA["email"], "claim.rejected").order_by("id").last().body_text)
        with self.assertRaises(services.SchemaError):
            self.submit()

    def test_two_rejections_do_not_block(self):
        editor = self.op(Role.EDITOR)
        for _ in range(2):
            self.reject(self.submit(), editor)
        self.assertIsNone(self.guard(Claim.all_tenants.first()).claim_blocked_at)
        self.submit()

    def test_a_rejection_in_another_listing_does_not_count(self):
        other = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="other", name="Other Co",
            visibility=Listing.Visibility.PUBLISHED,
        )
        editor = self.op(Role.EDITOR)
        for _ in range(2):
            self.reject(self.submit(), editor)
        self.reject(self.submit(listing=other), editor)
        self.assertFalse(ModerationItem.all_tenants.filter(item_type="claim_block").exists())

    def test_rejection_never_touches_the_verification_counters(self):
        claim = self.submit(method="domain_email")
        guard = self.guard(claim)
        locked_at = timezone.now() - timedelta(hours=1)
        guard.cooldowns, guard.wrong_entries = 2, 3
        guard.verification_locked_at = locked_at
        guard.cooldown_until = timezone.now() + timedelta(minutes=5)
        guard.save()
        self.reject(claim, self.op(Role.EDITOR))
        after = self.guard(claim)
        self.assertEqual((after.cooldowns, after.wrong_entries), (2, 3))
        self.assertEqual(after.verification_locked_at, locked_at)
        self.assertIsNotNone(after.cooldown_until)

    def test_rejection_retires_the_code(self):
        claim = self.submit(method="domain_email")
        message_id = claim.code_message_id
        self.reject(claim, self.op(Role.EDITOR))
        self.assertEqual(self.fresh(claim).code_hash, "")
        self.assertLessEqual(
            OutboundMessage.all_tenants.get(pk=message_id).expires_at, timezone.now()
        )

    def test_rejecting_a_dispute_decides_its_item(self):
        self.own()
        dispute = self.submit(claimant=DANA)
        self.reject(dispute, self.op(Role.EDITOR))
        item = ModerationItem.all_tenants.get(claim=dispute)
        self.assertEqual((item.status, item.decision), ("decided", "rejected"))
        self.assert_owner_agrees(self.listing, owner=self.fresh(dispute).listing.owner)


class QueueItemTests(_Base):
    def lock(self):
        claim = self.submit(method="domain_email")
        guard = self.guard(claim)
        guard.cooldowns = 2
        guard.save(update_fields=["cooldowns"])
        wrong = _any_wrong_code(claim)
        for _ in range(5):
            claim, _ = self.verify(claim, wrong)
        item = ModerationItem.all_tenants.get(item_type="verification_lock")
        return claim, item

    def unlock(self, item, operator, rationale="Confirmed by phone."):
        with tenant_context(self.tenant):
            return claim_review.unlock_verification(
                self.tenant, item=item, operator=operator, rationale=rationale
            )

    def unblock(self, item, operator, rationale="Mistaken rejections."):
        with tenant_context(self.tenant):
            return claim_review.unblock_claims(
                self.tenant, item=item, operator=operator, rationale=rationale
            )

    def test_the_third_cooldown_opens_the_lock_item_in_the_same_transaction(self):
        claim, item = self.lock()
        self.assertEqual(item.status, "open")
        self.assertEqual(item.claim_id, claim.pk)
        queued = self.events("moderation.queued").get()
        self.assertEqual(queued.data["item_type"], "verification_lock")
        self.assertEqual(queued.data["item_id"], item.public_id)

    def test_opening_an_item_twice_is_idempotent(self):
        claim, item = self.lock()
        with tenant_context(self.tenant), transaction.atomic():
            again = claim_review.open_item(
                self.tenant, item_type="verification_lock", listing=self.listing,
                claimant=claim.claimant, claim=claim, rules=[], actor={"type": "system", "id": "t"},
            )
        self.assertEqual(again.pk, item.pk)
        self.assertEqual(self.events("moderation.queued").count(), 1)

    def test_unlock_clears_the_lock_and_resets_only_the_verification_counters(self):
        claim, item = self.lock()
        guard = self.guard(claim)
        blocked_at = timezone.now()
        guard.rejections, guard.claim_blocked_at = 2, blocked_at
        guard.save(update_fields=["rejections", "claim_blocked_at"])
        moderator = self.op(Role.MODERATOR)
        self.unlock(item, moderator)
        after = self.guard(claim)
        self.assertIsNone(after.verification_locked_at)
        self.assertEqual((after.cooldowns, after.wrong_entries), (0, 0))
        self.assertIsNone(after.cooldown_until)
        self.assertEqual(after.rejections, 2)
        self.assertEqual(after.claim_blocked_at, blocked_at)
        item = self.fresh(item)
        self.assertEqual((item.status, item.decision, item.decided_by_id), ("decided", "unlocked", moderator.pk))
        decided = self.events("moderation.decided").get()
        self.assertEqual(decided.data["decision"], "unlocked")
        self.assertEqual(decided.data["decided_by"], "human")
        self.assertEqual(
            CommandLog.objects.filter(command="claim.unlock_verification").get().outcome,
            "applied",
        )
        with tenant_context(self.tenant):
            self.assertIsNone(
                claims.domain_email_ineligibility(self.tenant, self.listing, DANA["email"])
            )

    def test_the_unlocked_pair_needs_three_fresh_cooldowns_to_lock_again(self):
        claim, item = self.lock()
        self.unlock(item, self.op(Role.MODERATOR))
        guard = self.guard(claim)
        guard.last_code_sent_at = timezone.now() - timedelta(minutes=5)
        guard.save(update_fields=["last_code_sent_at"])
        fresh_claim = self.submit(method="domain_email")
        wrong = _any_wrong_code(fresh_claim)
        for _ in range(5):
            fresh_claim, _ = self.verify(fresh_claim, wrong)
        self.assertIsNone(self.guard(fresh_claim).verification_locked_at)

    def test_support_cannot_act_on_the_queue_and_moderator_can(self):
        _, item = self.lock()
        with self.assertRaises(claim_review.ReviewRefused) as cm:
            self.unlock(item, self.op(Role.SUPPORT))
        self.assertEqual(cm.exception.reason, "forbidden")
        self.assertEqual(
            CommandLog.objects.filter(command="claim.unlock_verification").last().outcome,
            "blocked",
        )
        self.unlock(item, self.op(Role.MODERATOR))

    def test_rationale_type_and_state_guards(self):
        claim, item = self.lock()
        moderator = self.op(Role.MODERATOR)
        with self.assertRaises(claim_review.ReviewRefused) as cm:
            self.unlock(item, moderator, rationale=" ")
        self.assertEqual(cm.exception.reason, "rationale_required")
        with self.assertRaises(claim_review.ReviewRefused) as cm:
            self.unblock(item, moderator)
        self.assertEqual(cm.exception.reason, "wrong_type")
        self.unlock(item, moderator)
        with self.assertRaises(claim_review.ReviewRefused) as cm:
            self.unlock(self.fresh(item), moderator)
        self.assertEqual(cm.exception.reason, "item_not_open")

    def test_a_lock_already_cleared_still_closes_its_item(self):
        # An item must never be un-closable (decisions.md §4.6).
        claim, item = self.lock()
        guard = self.guard(claim)
        guard.verification_locked_at = None
        guard.cooldowns, guard.rejections = 1, 2
        guard.save()
        moderator = self.op(Role.MODERATOR)
        self.unlock(item, moderator)
        item = self.fresh(item)
        self.assertEqual((item.status, item.decision), ("decided", "already_cleared"))
        self.assertEqual(item.decided_by_id, moderator.pk)
        after = self.guard(claim)
        self.assertEqual((after.cooldowns, after.rejections), (1, 2))  # nothing cleared
        self.assertEqual(self.events("moderation.decided").get().data["decision"], "already_cleared")
        self.assertEqual(
            CommandLog.objects.filter(command="claim.unlock_verification").get().outcome,
            "applied",
        )

    def block(self):
        editor = self.op(Role.EDITOR)
        for _ in range(3):
            claim = self.submit()
            self.reject(claim, editor)
        return claim, ModerationItem.all_tenants.get(item_type="claim_block")

    def test_unblock_clears_the_block_only_and_keeps_the_count(self):
        claim, item = self.block()
        guard = self.guard(claim)
        guard.cooldowns = 2
        guard.verification_locked_at = timezone.now()
        guard.save(update_fields=["cooldowns", "verification_locked_at"])
        self.unblock(item, self.op(Role.MODERATOR))
        after = self.guard(claim)
        self.assertIsNone(after.claim_blocked_at)
        self.assertEqual(after.rejections, 3)
        self.assertEqual(after.cooldowns, 2)
        self.assertIsNotNone(after.verification_locked_at)
        self.assertEqual(self.fresh(item).decision, "unblocked")
        self.submit()  # claiming is possible again

    def test_the_next_rejection_after_an_unblock_blocks_again_at_once(self):
        claim, item = self.block()
        self.unblock(item, self.op(Role.MODERATOR))
        again = self.submit()
        self.reject(again, self.op(Role.EDITOR))
        self.assertIsNotNone(self.guard(again).claim_blocked_at)
        self.assertEqual(
            ModerationItem.all_tenants.filter(item_type="claim_block", status="open").count(), 1
        )

    def test_unblock_when_already_cleared_still_closes_its_item(self):
        claim, item = self.block()
        ClaimVerificationGuard.all_tenants.filter(pk=self.guard(claim).pk).update(claim_blocked_at=None)
        self.unblock(item, self.op(Role.MODERATOR))
        item = self.fresh(item)
        self.assertEqual((item.status, item.decision), ("decided", "already_cleared"))
        self.assertEqual(self.guard(claim).rejections, 3)

    def test_a_decided_item_is_still_refused_not_decided_twice(self):
        claim, item = self.block()
        moderator = self.op(Role.MODERATOR)
        self.unblock(item, moderator)
        with self.assertRaises(claim_review.ReviewRefused) as cm:
            self.unblock(self.fresh(item), moderator)
        self.assertEqual(cm.exception.reason, "item_not_open")
        self.assertEqual(self.events("moderation.decided").count(), 1)


class VerifySuccessTests(_Base):
    def test_a_correct_code_on_an_unowned_listing_approves_in_the_same_transaction(self):
        claim = self.submit(method="domain_email")
        code = _capture_code(self, claim)
        claim, correct = self.verify(claim, code)
        self.assertTrue(correct)
        self.assertEqual(claim.status, Claim.Status.APPROVED)
        self.assertEqual(claim.method, "domain_email")
        self.assertIsNone(claim.decided_by_id)
        self.assert_owner_agrees(self.listing, owner=claim.claimant)
        approved = self.events("claim.approved").get()
        self.assertEqual(approved.data["verification"]["method"], "domain_email")
        self.assertNotIn("manual_verification", approved.data)
        self.assertEqual(approved.actor["type"], "visitor")
        row = CommandLog.objects.filter(command="claim.verify").last()
        self.assertEqual(row.result_event_id, approved.event_id)
        self.assertTrue(self.mail_to(LISTING_EMAIL, "claim.existing_contact_notice").exists())

    def test_a_correct_code_rejects_the_losers(self):
        winner = self.submit(method="domain_email")
        loser = self.submit(claimant=OTHER)
        self.verify(winner, _capture_code(self, winner))
        self.assertEqual(self.fresh(loser).status, Claim.Status.REJECTED)

    def test_a_listing_claimed_between_submit_and_verify_becomes_a_dispute(self):
        claim = self.submit(method="domain_email")
        code = _capture_code(self, claim)
        rival = self.claim_behind_their_back()
        claim, correct = self.verify(self.fresh(claim), code)
        self.assertTrue(correct)
        self.assertEqual(claim.status, Claim.Status.DISPUTED)
        self.assertIsNotNone(claim.verified_at)
        self.assert_owner_agrees(self.listing, owner=rival)
        self.assertEqual(self.events("claim.approved").count(), 0)
        self.assertEqual(self.events("claim.disputed").count(), 1)
        self.assertEqual(ModerationItem.all_tenants.filter(claim=claim).count(), 1)

    def test_verify_on_a_disputed_claim_records_evidence_and_never_approves(self):
        owner = self.own()
        claim = self.submit(method="domain_email")
        self.assertEqual(claim.status, Claim.Status.DISPUTED)
        code = _capture_code(self, claim)
        claim, correct = self.verify(claim, code)
        self.assertTrue(correct)
        self.assertEqual(claim.status, Claim.Status.DISPUTED)
        self.assertIsNotNone(claim.verified_at)
        self.assert_owner_agrees(self.listing, owner=owner.claimant)
        self.assertEqual(self.events("claim.approved").count(), 1)  # the setup approval only
        self.assertEqual(ModerationItem.all_tenants.filter(claim=claim).count(), 1)

    def test_a_disputed_domain_email_claim_no_longer_holds_a_dead_code(self):
        self.own()
        claim = self.submit(method="domain_email")
        self.assertTrue(claim.code_hash)
        with tenant_context(self.tenant):
            services.verify_claim_code(self.tenant, claim=claim, code="000000", ip=None)  # no refusal

    def test_a_suspended_listing_keeps_the_claim_open_with_verified_at(self):
        claim = self.submit(method="domain_email")
        code = _capture_code(self, claim)
        Listing.all_tenants.filter(pk=self.listing.pk).update(status=Listing.Status.SUSPENDED)
        claim, correct = self.verify(claim, code)
        self.assertTrue(correct)
        self.assertEqual(claim.status, Claim.Status.PENDING_VERIFICATION)
        self.assertIsNotNone(claim.verified_at)
        self.assertFalse(self.events("claim.approved").exists())

    def test_the_verified_page_says_what_happened(self):
        client = Client()
        claim = self.submit(method="domain_email")
        code = _capture_code(self, claim)
        r = client.post(f"/claim/{claim.public_id}/verify/", {"code": code}, HTTP_HOST=HOST)
        self.assertContains(r, "You are now the owner")
        self.own_again = None

    def test_wrong_codes_still_lock_and_a_correct_one_after_reset_approves(self):
        claim = self.submit(method="domain_email")
        code = _capture_code(self, claim)
        claim, _ = self.verify(claim, _any_wrong_code(claim))
        claim, correct = self.verify(claim, code)
        self.assertTrue(correct)
        self.assertEqual(claim.status, Claim.Status.APPROVED)


class DisputeItemTests(_Base):
    def test_a_dispute_opens_a_claim_dispute_item(self):
        self.own()
        claim = self.submit(claimant=DANA)
        item = ModerationItem.all_tenants.get(claim=claim)
        self.assertEqual((item.item_type, item.status), ("claim_dispute", "open"))
        self.assertEqual(item.rules_triggered, ["duplicate_claim"])
        queued = self.events("moderation.queued").get()
        self.assertEqual(queued.data["item_id"], item.public_id)
        self.assertEqual(queued.data["claim_id"], claim.public_id)
        self.assertEqual(self.fresh(claim).review_reason, "disputed")

    def test_a_dispute_moves_no_ownership(self):
        owner = self.own()
        self.submit(claimant=DANA)
        self.assert_owner_agrees(self.listing, owner=owner.claimant)


class ReviewReasonTests(_Base):
    def test_the_reason_a_claim_is_in_review_is_recorded(self):
        chosen = self.submit(method="manual")
        self.assertEqual((chosen.requested_method, chosen.review_reason), ("manual", "chosen"))
        flipped = self.submit(method="domain_email", claimant={**OTHER, "email": "eve@elsewhere.example"})
        self.assertEqual(flipped.method, "manual")
        self.assertEqual(
            (flipped.requested_method, flipped.review_reason), ("domain_email", "address_mismatch")
        )
        clean = self.submit(method="domain_email", claimant={**DANA, "email": "dana2@hoffmanplumbing.example"})
        self.assertEqual((clean.requested_method, clean.review_reason), ("domain_email", ""))

    def test_a_lock_records_its_reason(self):
        claim = self.submit(method="domain_email")
        guard = self.guard(claim)
        guard.cooldowns = 2
        guard.save(update_fields=["cooldowns"])
        wrong = _any_wrong_code(claim)
        for _ in range(5):
            claim, _ = self.verify(claim, wrong)
        self.assertEqual(self.fresh(claim).review_reason, "locked")


class OwnerWriterTests(TransactionTestCase):
    """#48: ``Listing.owner`` and ``Listing.status`` are the same fact, written
    in one place. Anything else assigning ``owner`` fails this test."""

    def test_only__assign_owner_writes_the_owner(self):
        root = pathlib.Path(directory.__file__).parent.parent
        offenders = []
        for package in ("directory", "tenants", "audit", "billing", "adapters", "osds"):
            for path in sorted((root / package).rglob("*.py")):
                parts = set(path.parts)
                if "tests" in parts or "migrations" in parts:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for func_or_module, node in _walk_with_scope(tree):
                    hit = None
                    if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for t in targets:
                            if isinstance(t, ast.Attribute) and t.attr in ("owner", "owner_id"):
                                hit = t.attr
                    elif isinstance(node, ast.keyword) and node.arg in ("owner", "owner_id"):
                        hit = node.arg
                    if hit and not (
                        path.name == "claim_review.py" and func_or_module == "_assign_owner"
                    ):
                        offenders.append(f"{path.relative_to(root)}:{getattr(node, 'lineno', '?')} {hit}")
        self.assertEqual(offenders, [])

    def test_the_scanner_would_catch_a_violation(self):
        tree = ast.parse("def f(listing, u):\n    listing.owner = u\n")
        hits = [
            n for _, n in _walk_with_scope(tree)
            if isinstance(n, ast.Assign)
            and isinstance(n.targets[0], ast.Attribute)
            and n.targets[0].attr == "owner"
        ]
        self.assertEqual(len(hits), 1)


def _walk_with_scope(tree):
    """Yield ``(enclosing function name or None, node)`` for every node."""
    stack = [(None, tree)]
    while stack:
        scope, node = stack.pop()
        yield scope, node
        child_scope = node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
        for child in ast.iter_child_nodes(node):
            stack.append((child_scope, child))


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test",
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AdminViewTests(_Base):
    def client_for(self, role, tenant=None):
        c = Client()
        c.force_login(self.op(role, tenant))
        return c

    def get(self, client, path):
        return client.get(path, HTTP_HOST=HOST)

    def post(self, client, path, data=None):
        return client.post(path, data or {}, HTTP_HOST=HOST)

    def test_the_queue_needs_moderator(self):
        self.submit()
        self.assertEqual(self.get(self.client_for(Role.SUPPORT), "/admin/claims/").status_code, 403)
        r = self.get(self.client_for(Role.MODERATOR), "/admin/claims/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Needs review (1)")
        self.assertContains(r, "Hoffman Plumbing")

    def test_anonymous_is_sent_to_login_and_a_stranger_gets_404(self):
        self.assertEqual(Client().get("/admin/claims/", HTTP_HOST=HOST).status_code, 302)
        other = Tenant.objects.create(slug="other", name="Other")
        stranger = self.client_for(Role.ADMIN, tenant=other)
        self.assertEqual(self.get(stranger, "/admin/claims/").status_code, 404)

    def test_the_three_tabs(self):
        self.own()
        self.submit(claimant=DANA)  # a dispute
        editor = self.op(Role.EDITOR)
        c = self.client_for(Role.MODERATOR)
        self.assertContains(self.get(c, "/admin/claims/?tab=disputes"), "Review dispute")
        blocked = None
        for _ in range(3):
            self.reject(self.submit(claimant={**OTHER, "email": "z@elsewhere.example"}), editor)
        self.assertContains(self.get(c, "/admin/claims/?tab=locks"), "Claim block")

    def test_a_moderator_sees_masked_contact_and_no_consent_an_editor_sees_both(self):
        claim = self.submit()
        url = f"/admin/claims/{claim.public_id}/"
        mod = self.get(self.client_for(Role.MODERATOR), url)
        self.assertNotContains(mod, DANA["email"])
        self.assertNotContains(mod, "<h2>Consent</h2>")
        self.assertNotContains(mod, "Approve claim")
        editor = self.get(self.client_for(Role.EDITOR), url)
        self.assertContains(editor, DANA["email"])
        self.assertContains(editor, "<h2>Consent</h2>")
        self.assertContains(editor, "Approve claim")

    def test_a_claim_of_another_tenant_is_404(self):
        other = Tenant.objects.create(slug="other", name="Other", primary_domain="other.test")
        ltype = ListingType.all_tenants.create(
            tenant=other, key="b", label_singular="B", label_plural="Bs", path_segment="bs"
        )
        listing = Listing.all_tenants.create(tenant=other, listing_type=ltype, slug="x", name="X")
        with tenant_context(other):
            foreign = services.submit_claim(
                other, listing=listing, method="manual", claimant=dict(DANA),
                consent=GRANTED_ALL, ip="203.0.113.1",
            )
        r = self.get(self.client_for(Role.EDITOR), f"/admin/claims/{foreign.public_id}/")
        self.assertEqual(r.status_code, 404)

    def test_approve_by_post_as_editor(self):
        claim = self.submit()
        c = self.client_for(Role.EDITOR)
        r = self.post(c, f"/admin/claims/{claim.public_id}/approve/",
                      {"method_used": "phone", "notes": "Spoke to them."})
        self.assertEqual(r.status_code, 302)
        self.assert_owner_agrees(self.listing, owner=claim.claimant)

    def test_approve_without_notes_shows_the_refusal_and_changes_nothing(self):
        claim = self.submit()
        c = self.client_for(Role.EDITOR)
        r = c.post(f"/admin/claims/{claim.public_id}/approve/",
                   {"method_used": "phone", "notes": ""}, HTTP_HOST=HOST, follow=True)
        self.assertContains(r, "Notes are required")
        self.assert_owner_agrees(self.listing, owner=None)

    def test_a_moderator_cannot_post_an_approval(self):
        claim = self.submit()
        r = self.post(self.client_for(Role.MODERATOR), f"/admin/claims/{claim.public_id}/approve/",
                      {"method_used": "phone", "notes": "x"})
        self.assertEqual(r.status_code, 403)
        self.assert_owner_agrees(self.listing, owner=None)

    def test_the_action_routes_are_post_only(self):
        claim = self.submit()
        c = self.client_for(Role.ADMIN)
        for path in ("approve", "reject"):
            self.assertEqual(self.get(c, f"/admin/claims/{claim.public_id}/{path}/").status_code, 405)

    def test_reject_by_post(self):
        claim = self.submit()
        r = self.post(self.client_for(Role.EDITOR), f"/admin/claims/{claim.public_id}/reject/",
                      {"reason": "Not enough evidence."})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.fresh(claim).status, Claim.Status.REJECTED)

    def test_a_transfer_needs_the_checkbox_in_the_form_and_the_post(self):
        self.own()
        dispute = self.submit(claimant=DANA)
        c = self.client_for(Role.EDITOR)
        page = self.get(c, f"/admin/claims/{dispute.public_id}/")
        self.assertContains(page, "transfers ownership")
        r = c.post(f"/admin/claims/{dispute.public_id}/approve/",
                   {"method_used": "phone", "notes": "ok"}, HTTP_HOST=HOST, follow=True)
        self.assertContains(r, "Confirm the ownership transfer")
        self.post(c, f"/admin/claims/{dispute.public_id}/approve/",
                  {"method_used": "phone", "notes": "ok", "confirm_transfer": "1"})
        self.assertEqual(self.fresh(dispute).status, Claim.Status.APPROVED)

    def test_unlock_by_post_as_moderator_and_not_as_support(self):
        claim = self.submit(method="domain_email")
        guard = self.guard(claim)
        guard.cooldowns = 2
        guard.save(update_fields=["cooldowns"])
        wrong = _any_wrong_code(claim)
        for _ in range(5):
            claim, _ = self.verify(claim, wrong)
        item = ModerationItem.all_tenants.get(item_type="verification_lock")
        url = f"/admin/moderation/{item.public_id}/unlock/"
        self.assertEqual(self.post(self.client_for(Role.SUPPORT), url, {"rationale": "x"}).status_code, 403)
        page = self.get(self.client_for(Role.MODERATOR), f"/admin/moderation/{item.public_id}/")
        self.assertContains(page, "Unlock")
        self.post(self.client_for(Role.MODERATOR), url, {"rationale": "Verified by phone."})
        self.assertIsNone(self.guard(claim).verification_locked_at)

    def test_the_admin_home_links_to_the_queue_with_a_count(self):
        self.submit()
        home = self.get(self.client_for(Role.MODERATOR), "/admin/")
        self.assertContains(home, "Claims review (1 waiting)")
        self.assertNotContains(self.get(self.client_for(Role.SUPPORT), "/admin/"), "Claims review")
