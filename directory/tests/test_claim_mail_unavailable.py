"""The fourth ``domain_email`` ineligibility reason: mail unavailable
(decisions.md §4.5), and the rule that every flip to ``manual`` clears the
code and expires the pending code message.

TransactionTestCase throughout, for the same reason as test_claim_
verification.py -- the claim commands refuse an open transaction.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, TransactionTestCase
from django.utils import timezone

from audit.models import CommandLog, OutboundMessage
from audit.worker.mail_drain import mail_drain_once
from directory import claims, services
from directory.forms import ClaimForm
from directory.models import (
    ClaimVerificationGuard,
    DirectoryUser,
    Listing,
    ListingType,
)
from directory.tests.test_claim_verification import (
    DANA,
    GRANTED_ALL,
    HOST,
    _any_wrong_code,
    _Base as VerificationBase,
    _domain_email_enabled,
)
from osds.adapters import override_capability
from osds.tenancy import tenant_context
from osds.tests.mail_stub import email_send_stub
from tenants.models import InstallSetup, Tenant

MAIL_DOWN = "Email verification is unavailable for this directory right now"


class SubmitMailUnavailableTests(VerificationBase):
    def setUp(self):
        super().setUp()
        self.mail._available = False

    def test_submit_flips_to_manual_with_no_code_and_no_message(self):
        claim = self.submit()
        self.assertEqual(claim.method, "manual")
        self.assertFalse(claim.code_hash)
        self.assertIsNone(claim.code_message_id)
        self.assertFalse(OutboundMessage.all_tenants.filter(tenant=self.tenant).exists())
        self.assertFalse(self.events("claim.verification_started").exists())
        self.assertEqual(
            self.events("claim.submitted").first().data["claim"]["method"], "manual"
        )

    def test_submit_burns_no_send_cap(self):
        claim = self.submit()
        self.assertFalse(
            ClaimVerificationGuard.all_tenants.filter(
                tenant=self.tenant, listing=self.listing, claimant=claim.claimant,
                codes_sent_in_window__gt=0,
            ).exists()
        )

    def test_submit_concludes_applied(self):
        self.submit()
        row = CommandLog.objects.filter(command="claim.submit").order_by("id").last()
        self.assertEqual(row.outcome, "applied")

    def test_no_provider_registered_is_unavailable_too(self):
        with override_capability({}):
            claim = self.submit()
        self.assertEqual(claim.method, "manual")

    def test_a_provider_without_available_fails_closed(self):
        class Bare:
            def send(self, ctx, message):
                raise AssertionError("never sent")

        with override_capability({"email.send": Bare()}):
            claim = self.submit()
        self.assertEqual(claim.method, "manual")

    def test_manual_method_is_unaffected(self):
        claim = self.submit(method="manual")
        self.assertEqual(claim.method, "manual")

    def test_claim_form_still_offers_domain_email(self):
        form = ClaimForm(listing=self.listing, enabled_methods=["manual", "domain_email"])
        self.assertIn("domain_email", [v for v, _ in form.fields["method"].choices])


class IneligibilityOrderTests(VerificationBase):
    def reason(self, email=DANA["email"]):
        with tenant_context(self.tenant):
            return claims.domain_email_ineligibility(self.tenant, self.listing, email)

    def test_eligible_is_none(self):
        self.assertIsNone(self.reason())

    def test_no_website_outranks_everything(self):
        self.listing.website = ""
        self.mail._available = False
        self.assertEqual(self.reason("x@elsewhere.example"), "no_website")

    def test_locked_outranks_mail_unavailable(self):
        user = DirectoryUser.all_tenants.create(tenant=self.tenant, email=DANA["email"])
        ClaimVerificationGuard.all_tenants.create(
            tenant=self.tenant, listing=self.listing, claimant=user,
            verification_locked_at=timezone.now(),
        )
        self.mail._available = False
        self.assertEqual(self.reason(), "locked")

    def test_mail_unavailable_outranks_address_mismatch(self):
        self.mail._available = False
        self.assertEqual(self.reason("dana@gmail.example"), "mail_unavailable")

    def test_address_mismatch_when_mail_is_up(self):
        self.assertEqual(self.reason("dana@gmail.example"), "address_mismatch")

    def test_domain_email_eligible_is_unchanged_by_mail_state(self):
        self.mail._available = False
        with tenant_context(self.tenant):
            self.assertTrue(claims.domain_email_eligible(self.listing, DANA["email"]))


class FlipToManualTests(VerificationBase):
    def _send_then_take_mail_down(self):
        claim = self.submit()
        self.assertTrue(claim.code_hash)
        self.mail._available = False
        return claim

    def test_resend_flips_to_manual_and_emits_ineligible(self):
        claim = self._send_then_take_mail_down()
        claim = self.resend(claim)
        claim.refresh_from_db()
        self.assertEqual(claim.method, "manual")
        failed = self.events("claim.verification_failed").order_by("id").last()
        self.assertEqual(failed.data["reason"], "ineligible")
        row = (
            CommandLog.objects.filter(command="claim.start_verification")
            .order_by("id").last()
        )
        self.assertEqual(row.outcome, "applied")
        self.assertEqual(row.result_event_id, failed.event_id)

    def test_flip_clears_code_hash_and_expires_the_pending_message(self):
        claim = self._send_then_take_mail_down()
        message_id = claim.code_message_id
        claim = self.resend(claim)
        claim.refresh_from_db()
        self.assertEqual(claim.code_hash, "")
        message = OutboundMessage.all_tenants.get(pk=message_id)
        self.assertLessEqual(message.expires_at, timezone.now())
        # Left pending: the drain expires it. It is not dead-lettered.
        self.assertEqual(message.status, OutboundMessage.Status.PENDING)

    def test_a_sent_message_is_left_alone(self):
        claim = self._send_then_take_mail_down()
        OutboundMessage.all_tenants.filter(pk=claim.code_message_id).update(
            status=OutboundMessage.Status.SENT
        )
        before = OutboundMessage.all_tenants.get(pk=claim.code_message_id).expires_at
        self.resend(claim)
        after = OutboundMessage.all_tenants.get(pk=claim.code_message_id).expires_at
        self.assertEqual(before, after)

    def test_flip_for_another_reason_also_clears_and_expires(self):
        claim = self.submit()
        message_id = claim.code_message_id
        self.listing.website = ""
        self.listing.save(update_fields=["website"])
        claim = self.resend(claim)
        claim.refresh_from_db()
        self.assertEqual((claim.method, claim.code_hash), ("manual", ""))
        self.assertLessEqual(
            OutboundMessage.all_tenants.get(pk=message_id).expires_at, timezone.now()
        )

    def test_lock_flip_expires_the_pending_message(self):
        claim = self.submit()
        message_id = claim.code_message_id
        guard = self.guard(claim)
        guard.cooldowns = 2
        guard.save(update_fields=["cooldowns"])
        wrong = _any_wrong_code(claim)
        for _ in range(5):
            claim, _ = self.verify(claim, wrong)
        claim.refresh_from_db()
        self.assertEqual((claim.method, claim.code_hash), ("manual", ""))
        self.assertLessEqual(
            OutboundMessage.all_tenants.get(pk=message_id).expires_at, timezone.now()
        )

    def test_a_flip_never_delivers_the_old_code_after_mail_returns(self):
        claim = self._send_then_take_mail_down()
        self.resend(claim)
        self.mail._available = True
        stats = mail_drain_once(now=timezone.now() + timedelta(seconds=1))
        self.assertEqual(self.mail.sent, [])
        self.assertEqual(stats.sent, 0)

    def test_resend_with_mail_available_is_unchanged(self):
        claim = self.submit()
        guard = self.guard(claim)
        guard.last_code_sent_at = timezone.now() - timedelta(seconds=120)
        guard.save(update_fields=["last_code_sent_at"])
        claim = self.resend(claim)
        claim.refresh_from_db()
        self.assertEqual(claim.method, "domain_email")
        self.assertTrue(claim.code_hash)


class SubmittedPageCopyTests(TransactionTestCase):
    def setUp(self):
        self.mail = self.enterContext(email_send_stub())
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain=HOST,
            domain_verified_at=timezone.now(), settings=_domain_email_enabled(),
        )
        lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="Business",
            label_plural="Businesses", path_segment="businesses",
        )
        self.listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug="hoffman-plumbing",
            name="Hoffman Plumbing", visibility=Listing.Visibility.PUBLISHED,
            website="https://hoffmanplumbing.example",
        )
        self.client = Client()

    def _submit(self, email=DANA["email"]):
        data = {
            "name": DANA["name"], "email": email, "phone_e164": "",
            "role_claimed": "owner", "method": "domain_email",
            "marketing_email": "on", "marketing_sms": "", "automated_calls": "",
        }
        r = self.client.post(f"/claim/{self.listing.public_id}/", data, HTTP_HOST=HOST)
        self.assertIn("/submitted/", r.url)
        return r

    def _view(self, email=DANA["email"]):
        return self.client.get(self._submit(email).url, HTTP_HOST=HOST)

    def test_mail_unavailable_copy(self):
        self.mail._available = False
        r = self._view()
        self.assertContains(r, MAIL_DOWN)
        self.assertNotContains(r, "must be at @")

    def test_mail_unavailable_outranks_a_mismatched_address(self):
        self.mail._available = False
        r = self._view("dana@gmail.example")
        self.assertContains(r, MAIL_DOWN)
        self.assertNotContains(r, "must be at @")

    def test_mismatch_copy_when_mail_is_up(self):
        r = self._view("dana@gmail.example")
        self.assertContains(r, "must be at @hoffmanplumbing.example")
        self.assertNotContains(r, MAIL_DOWN)

    def test_no_website_copy_outranks_mail_unavailable(self):
        # The form never offers domain_email for a website-less listing, so
        # this goes through the service, as an import-emptied website would.
        self.mail._available = False
        self.listing.website = ""
        self.listing.save(update_fields=["website"])
        with tenant_context(self.tenant):
            claim = services.submit_claim(
                self.tenant, listing=self.listing, method="domain_email",
                claimant=dict(DANA), consent=GRANTED_ALL, ip="203.0.113.44",
            )
        self.assertEqual(claim.review_reason, "no_website")
        r = self.client.get(f"/claim/{claim.public_id}/submitted/", HTTP_HOST=HOST)
        self.assertContains(r, "no eligible website")
        self.assertNotContains(r, MAIL_DOWN)

    def test_the_page_reads_the_stored_reason_not_the_current_state(self):
        # Mail configured after submit: the claim still went to review because
        # mail was down, and the page says so.
        self.mail._available = False
        url = self._submit().url
        self.mail._available = True
        page = self.client.get(url, HTTP_HOST=HOST)
        self.assertContains(page, MAIL_DOWN)
        self.assertNotContains(page, "must be at @")

    def test_a_website_removed_after_submit_does_not_change_the_copy(self):
        self.mail._available = False
        url = self._submit().url
        self.listing.website = ""
        self.listing.save(update_fields=["website"])
        page = self.client.get(url, HTTP_HOST=HOST)
        self.assertContains(page, MAIL_DOWN)
        self.assertNotContains(page, "no eligible website")

    def test_a_claim_that_asked_for_manual_shows_no_reason(self):
        data = {
            "name": DANA["name"], "email": DANA["email"], "phone_e164": "",
            "role_claimed": "owner", "method": "manual",
            "marketing_email": "on", "marketing_sms": "", "automated_calls": "",
        }
        r = self.client.post(f"/claim/{self.listing.public_id}/", data, HTTP_HOST=HOST)
        page = self.client.get(r.url, HTTP_HOST=HOST)
        self.assertNotContains(page, MAIL_DOWN)
        self.assertNotContains(page, "wasn't")
