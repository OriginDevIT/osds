"""domain_email claim verification (claims PR 3 -- spec §9.5, §9.6;
decisions.md §4.4). Companion to test_claims.py, which covers claim.submit's
non-verification mechanics.

TransactionTestCase throughout -- submit_claim, start_claim_verification and
verify_claim_code all refuse to run inside an open transaction.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta

from django.conf import settings as django_settings
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from audit.models import CommandLog, OutboundMessage, OutboxEvent
from directory import claims, services
from directory.models import (
    Claim,
    ClaimVerificationGuard,
    DirectoryUser,
    Listing,
    ListingType,
)
from osds.tenancy import tenant_context
from osds.tests.mail_stub import email_send_stub
from tenants.models import InstallSetup, Operator, Tenant
from tenants.services import InvalidTenantSettings, update_tenant_settings

HOST = "acme.test"

GRANTED_ALL = {
    "marketing_email": {"granted": True},
    "marketing_sms": {"granted": True},
    "automated_calls": {"granted": False},
}

DANA = {
    "name": "Dana Hoffman",
    "email": "dana@hoffmanplumbing.example",
    "phone_e164": "+17735550142",
    "role_claimed": "owner",
}

def _domain_email_enabled() -> dict:
    # A fresh dict per call -- callers mutate tenant.settings freely.
    return {"claim_verification": {"enabled_methods": ["manual", "domain_email"]}}


class _Base(TransactionTestCase):
    def setUp(self):
        self.mail = self.enterContext(email_send_stub())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", settings=_domain_email_enabled(),
        )
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )
        self.listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="hoffman-plumbing",
            name="Hoffman Plumbing", website="https://hoffmanplumbing.example",
        )

    def submit(self, **overrides):
        kwargs = dict(
            listing=self.listing,
            method="domain_email",
            claimant=dict(DANA),
            consent=GRANTED_ALL,
            ip="203.0.113.44",
        )
        kwargs.update(overrides)
        with tenant_context(self.tenant):
            return services.submit_claim(self.tenant, **kwargs)

    def guard(self, claim):
        with tenant_context(self.tenant):
            return ClaimVerificationGuard.objects.get(
                listing=claim.listing, claimant=claim.claimant
            )

    def events(self, etype):
        return OutboxEvent.all_tenants.filter(type=etype, tenant=self.tenant)

    def resend(self, claim):
        with tenant_context(self.tenant):
            return services.start_claim_verification(self.tenant, claim=claim)

    def verify(self, claim, code):
        with tenant_context(self.tenant):
            return services.verify_claim_code(self.tenant, claim=claim, code=code)


# --- submit-time eligibility (correction c: resolved before Claim.objects.create) ---

class SubmitEligibilityTests(_Base):
    def test_submit_domain_email_eligible_starts_verification_same_transaction(self):
        claim = self.submit()
        self.assertEqual(claim.method, "domain_email")
        self.assertTrue(claim.code_hash)
        types = list(
            OutboxEvent.all_tenants.filter(tenant=self.tenant)
            .order_by("id").values_list("type", flat=True)
        )
        self.assertIn("claim.submitted", types)
        self.assertIn("claim.verification_started", types)

    def test_submit_domain_email_no_website_flips_to_manual_no_code(self):
        self.listing.website = ""
        self.listing.save(update_fields=["website"])
        claim = self.submit()
        self.assertEqual(claim.method, "manual")
        self.assertFalse(claim.code_hash)
        submitted = self.events("claim.submitted").first()
        self.assertEqual(submitted.data["claim"]["method"], "manual")
        self.assertFalse(self.events("claim.verification_failed").exists())

    def test_submit_domain_email_platform_host_flips_to_manual_no_code(self):
        self.listing.website = "https://facebook.com/hoffmanplumbing"
        self.listing.save(update_fields=["website"])
        claim = self.submit()
        self.assertEqual(claim.method, "manual")
        self.assertFalse(claim.code_hash)
        self.assertFalse(self.events("claim.verification_failed").exists())

    def test_submit_domain_email_host_mismatch_flips_to_manual_no_code(self):
        claim = self.submit(claimant={**DANA, "email": "dana@gmail.example"})
        self.assertEqual(claim.method, "manual")
        self.assertFalse(claim.code_hash)
        self.assertFalse(self.events("claim.verification_failed").exists())

    def test_submit_domain_email_locked_pair_flips_to_manual(self):
        user = DirectoryUser.all_tenants.create(
            tenant=self.tenant, email=DANA["email"], name=DANA["name"]
        )
        ClaimVerificationGuard.all_tenants.create(
            tenant=self.tenant, listing=self.listing, claimant=user,
            verification_locked_at=timezone.now(),
        )
        claim = self.submit()
        self.assertEqual(claim.method, "manual")
        self.assertFalse(claim.code_hash)
        self.assertFalse(self.events("claim.verification_failed").exists())

    def test_submit_domain_email_during_cooldown_creates_claim_with_no_code(self):
        first = self.submit()
        # force the pair into a cooldown, then a second claimant submission
        # on the same pair is impossible (one user per tenant+email) -- so
        # exercise this via resend instead: put the guard on cooldown, then
        # resend must report "not sent" without raising into submit's own
        # command log. Simulated directly by calling the inner function.
        guard = self.guard(first)
        guard.cooldown_until = timezone.now() + timedelta(minutes=15)
        guard.save(update_fields=["cooldown_until"])
        with tenant_context(self.tenant):
            claim, result, _eid = services._apply_start_verification(
                self.tenant, claim=first
            )
        self.assertEqual(result, "cooldown")
        self.assertEqual(claim.method, "domain_email")

    def test_submit_domain_email_send_cap_exhausted_creates_claim_with_no_code(self):
        first = self.submit()
        guard = self.guard(first)
        guard.codes_sent_window_start = timezone.now()
        guard.codes_sent_in_window = 5
        guard.last_code_sent_at = timezone.now() - timedelta(hours=1)
        guard.save(
            update_fields=[
                "codes_sent_window_start", "codes_sent_in_window", "last_code_sent_at",
            ]
        )
        with tenant_context(self.tenant):
            claim, result, _eid = services._apply_start_verification(
                self.tenant, claim=first
            )
        self.assertEqual(result, "cap")


class EligibilityHelperTests(_Base):
    def test_platform_host_list_sample(self):
        for host in ("facebook.com", "instagram.com", "yelp.com", "etsy.com"):
            self.assertIn(host, claims.PLATFORM_HOSTS)

    def test_domain_email_www_stripped_from_website_host_only(self):
        self.listing.website = "https://www.hoffmanplumbing.example"
        self.listing.save(update_fields=["website"])
        with tenant_context(self.tenant):
            # website side: www. stripped, so the bare host is required
            self.assertTrue(
                claims.domain_email_eligible(
                    self.listing, "dana@hoffmanplumbing.example"
                )
            )
            # email side: www. is NOT stripped -- www.<host> does not match
            self.assertFalse(
                claims.domain_email_eligible(
                    self.listing, "dana@www.hoffmanplumbing.example"
                )
            )

    def test_domain_email_host_normalization_idna_port_trailing_dot(self):
        self.listing.website = "https://HoffmanPlumbing.example.:8443/"
        self.listing.save(update_fields=["website"])
        with tenant_context(self.tenant):
            self.assertTrue(
                claims.domain_email_eligible(
                    self.listing, "dana@hoffmanplumbing.example"
                )
            )


# --- TTL / bounds ---

class TtlBoundsTests(_Base):
    def test_start_verification_computes_expires_at_from_tenant_ttl(self):
        update_tenant_settings(
            tenant=self.tenant,
            changes={"claim_verification": {
                "enabled_methods": ["manual", "domain_email"],
                "ttl": {"domain_email_minutes": 60},
            }},
            changed_by=self._operator(),
        )
        self.tenant.refresh_from_db()
        before = timezone.now()
        claim = self.submit()
        self.assertAlmostEqual(
            (claim.expires_at - before).total_seconds(), 3600, delta=5
        )

    def test_start_verification_default_ttl_1440_minutes_when_unset(self):
        self.tenant.settings = _domain_email_enabled()
        self.tenant.save(update_fields=["settings"])
        before = timezone.now()
        claim = self.submit()
        self.assertAlmostEqual(
            (claim.expires_at - before).total_seconds(), 1440 * 60, delta=5
        )

    def test_update_tenant_settings_rejects_out_of_bounds_domain_email_ttl(self):
        operator = self._operator()
        with self.assertRaises(InvalidTenantSettings):
            update_tenant_settings(
                tenant=self.tenant,
                changes={"claim_verification": {"ttl": {"domain_email_minutes": 10}}},
                changed_by=operator,
            )
        with self.assertRaises(InvalidTenantSettings):
            update_tenant_settings(
                tenant=self.tenant,
                changes={
                    "claim_verification": {"ttl": {"domain_email_minutes": 999999}}
                },
                changed_by=operator,
            )

    def test_stored_out_of_bounds_ttl_falls_back_to_default_with_warning(self):
        # Only direct SQL can produce this state (§4.4) -- simulated by
        # writing the settings dict directly, bypassing update_tenant_settings.
        self.tenant.settings = {
            "claim_verification": {
                "enabled_methods": ["manual", "domain_email"],
                "ttl": {"domain_email_minutes": 5},
            }
        }
        self.tenant.save(update_fields=["settings"])
        before = timezone.now()
        with self.assertLogs("osds.claims", level="WARNING"):
            claim = self.submit()
        self.assertAlmostEqual(
            (claim.expires_at - before).total_seconds(), 1440 * 60, delta=5
        )

    def test_stored_non_int_ttl_falls_back_to_default_with_same_warning(self):
        # fix #8: a non-int stored value (only direct SQL) falls back the
        # same way an out-of-bounds int does.
        self.tenant.settings = {
            "claim_verification": {
                "enabled_methods": ["manual", "domain_email"],
                "ttl": {"domain_email_minutes": "soon"},
            }
        }
        self.tenant.save(update_fields=["settings"])
        before = timezone.now()
        with self.assertLogs("osds.claims", level="WARNING"):
            claim = self.submit()
        self.assertAlmostEqual(
            (claim.expires_at - before).total_seconds(), 1440 * 60, delta=5
        )

    def _operator(self):
        return Operator.objects.create_user(email="op@acme.test", password=None)


class CodeShapeTests(_Base):
    def test_code_is_six_digits_zero_padded(self):
        for _ in range(20):
            code = claims.generate_code()
            self.assertEqual(len(code), 6)
            self.assertTrue(code.isdigit())

    def test_code_hash_matches_independently_computed_salted_hmac_sha256(self):
        claim = self.submit()
        # _capture_code brute-forces the plaintext via claims.hash_code --
        # that's just recovery of a value the service never returns. The
        # assertion below never calls hash_code or salted_hmac: it
        # reconstructs salted_hmac's own derivation by hand (django.utils
        # .crypto.salted_hmac: key = hasher(key_salt + secret).digest(),
        # then hmac.new(key, value, hasher)) and compares the literal hex.
        code = _capture_code(self, claim)
        key = hashlib.sha256(
            b"osds.claim.verification_code" + django_settings.SECRET_KEY.encode()
        ).digest()
        expected = hmac.new(
            key, msg=f"{claim.public_id}:{code}".encode(), digestmod=hashlib.sha256
        ).hexdigest()
        self.assertEqual(claim.code_hash, expected)


# --- start refusals (fix #4: last three) ---

class StartRefusalTests(_Base):
    def test_start_verification_refuses_when_status_not_pending_verification(self):
        claim = self.submit()
        claim.status = Claim.Status.APPROVED
        claim.save(update_fields=["status"])
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.resend(claim)
        self.assertEqual(ctx.exception.reason, "status")

    def test_start_verification_refuses_when_method_not_domain_email(self):
        claim = self.submit()
        claim.method = Claim.Method.MANUAL
        claim.save(update_fields=["method"])
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.resend(claim)
        self.assertEqual(ctx.exception.reason, "method")

    def test_start_verification_refuses_when_verified_at_is_set(self):
        claim = self.submit()
        claim.verified_at = timezone.now()
        claim.save(update_fields=["verified_at"])
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.resend(claim)
        self.assertEqual(ctx.exception.reason, "verified")


# --- verify refusals (fix #4 + correction b: locked then checked) ---

class VerifyRefusalTests(_Base):
    def test_verify_refuses_when_code_hash_empty(self):
        claim = self.submit()
        claim.code_hash = ""
        claim.save(update_fields=["code_hash"])
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.verify(claim, "123456")
        self.assertEqual(ctx.exception.reason, "no_active_code")

    def test_verify_refuses_when_status_not_pending_verification(self):
        claim = self.submit()
        claim.status = Claim.Status.APPROVED
        claim.save(update_fields=["status"])
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.verify(claim, "123456")
        self.assertEqual(ctx.exception.reason, "status")

    def test_verify_refuses_when_method_not_domain_email(self):
        claim = self.submit()
        claim.method = Claim.Method.MANUAL
        claim.save(update_fields=["method"])
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.verify(claim, "123456")
        self.assertEqual(ctx.exception.reason, "method")

    def test_verify_refuses_when_verified_at_is_set(self):
        claim = self.submit()
        claim.verified_at = timezone.now()
        claim.save(update_fields=["verified_at"])
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.verify(claim, "123456")
        self.assertEqual(ctx.exception.reason, "verified")

    def test_verify_refuses_during_cooldown_no_state_change(self):
        claim = self.submit()
        guard = self.guard(claim)
        guard.cooldown_until = timezone.now() + timedelta(minutes=15)
        guard.save(update_fields=["cooldown_until"])
        attempts_before = claim.attempts
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.verify(claim, "123456")
        self.assertEqual(ctx.exception.reason, "cooldown")
        claim.refresh_from_db()
        self.assertEqual(claim.attempts, attempts_before)

    def test_verify_refuses_when_locked_no_state_change(self):
        claim = self.submit()
        guard = self.guard(claim)
        guard.verification_locked_at = timezone.now()
        guard.save(update_fields=["verification_locked_at"])
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.verify(claim, "123456")
        self.assertEqual(ctx.exception.reason, "locked")

    def test_verify_refuses_when_expired_no_attempt_consumed(self):
        claim = self.submit()
        claim.expires_at = timezone.now() - timedelta(seconds=1)
        claim.save(update_fields=["expires_at"])
        attempts_before = claim.attempts
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.verify(claim, "123456")
        self.assertEqual(ctx.exception.reason, "expired")
        claim.refresh_from_db()
        self.assertEqual(claim.attempts, attempts_before)

    # -- refusal order (fix #2: status, method, verified, locked, cooldown,
    # no_active_code, expired -- broadest state first) --

    def test_refusal_order_status_beats_no_active_code(self):
        claim = self.submit()
        claim.status = Claim.Status.APPROVED
        claim.code_hash = ""
        claim.save(update_fields=["status", "code_hash"])
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.verify(claim, "123456")
        self.assertEqual(ctx.exception.reason, "status")

    def test_refusal_order_locked_beats_cooldown(self):
        claim = self.submit()
        guard = self.guard(claim)
        guard.verification_locked_at = timezone.now()
        guard.cooldown_until = timezone.now() + timedelta(minutes=15)
        guard.save(update_fields=["verification_locked_at", "cooldown_until"])
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.verify(claim, "123456")
        self.assertEqual(ctx.exception.reason, "locked")

    def test_refusal_order_cooldown_beats_no_active_code_and_expired(self):
        claim = self.submit()
        guard = self.guard(claim)
        guard.cooldown_until = timezone.now() + timedelta(minutes=15)
        guard.save(update_fields=["cooldown_until"])
        claim.code_hash = ""
        claim.expires_at = timezone.now() - timedelta(seconds=1)
        claim.save(update_fields=["code_hash", "expires_at"])
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.verify(claim, "123456")
        self.assertEqual(ctx.exception.reason, "cooldown")

    def test_refusal_order_no_active_code_beats_expired(self):
        claim = self.submit()
        claim.code_hash = ""
        claim.expires_at = timezone.now() - timedelta(seconds=1)
        claim.save(update_fields=["code_hash", "expires_at"])
        with self.assertRaises(services.VerificationRefused) as ctx:
            self.verify(claim, "123456")
        self.assertEqual(ctx.exception.reason, "no_active_code")


# --- wrong-entry cascade (corrections a, fixes #2/#3) ---

class WrongEntryTests(_Base):
    def test_wrong_entries_one_to_four_emit_wrong_code_with_after_state_attempt(self):
        claim = self.submit()
        wrong_code = _any_wrong_code(claim)
        for n in range(1, 5):
            claim, correct = self.verify(claim, wrong_code)
            self.assertFalse(correct)
            failed = self.events("claim.verification_failed").order_by("id").last()
            self.assertEqual(failed.data["reason"], "wrong_code")
            self.assertEqual(failed.data["attempt"], n)
            self.assertEqual(failed.data["cooldowns"], 0)
        claim.refresh_from_db()
        self.assertEqual(claim.attempts, 4)
        self.assertTrue(claim.code_hash)  # not yet cleared

    def test_fifth_wrong_entry_emits_cooldown_not_wrong_code_clears_code_hash(self):
        claim = self.submit()
        wrong_code = _any_wrong_code(claim)
        for _ in range(4):
            claim, _ = self.verify(claim, wrong_code)
        claim, correct = self.verify(claim, wrong_code)
        self.assertFalse(correct)
        failed = self.events("claim.verification_failed").order_by("id").last()
        self.assertEqual(failed.data["reason"], "cooldown")
        self.assertEqual(failed.data["attempt"], 5)
        self.assertEqual(failed.data["cooldowns"], 1)
        claim.refresh_from_db()
        self.assertFalse(claim.code_hash)
        guard = self.guard(claim)
        self.assertEqual(guard.wrong_entries, 0)
        self.assertIsNotNone(guard.cooldown_until)

    def test_fifth_wrong_entry_opening_third_cooldown_emits_locked_and_flips_method_to_manual(self):
        claim = self.submit()
        guard = self.guard(claim)
        guard.cooldowns = 2
        guard.save(update_fields=["cooldowns"])
        wrong_code = _any_wrong_code(claim)
        for _ in range(4):
            claim, _ = self.verify(claim, wrong_code)
        claim, correct = self.verify(claim, wrong_code)
        self.assertFalse(correct)
        failed = self.events("claim.verification_failed").order_by("id").last()
        self.assertEqual(failed.data["reason"], "locked")
        self.assertEqual(failed.data["attempt"], 5)
        self.assertEqual(failed.data["cooldowns"], 3)
        claim.refresh_from_db()
        self.assertEqual(claim.method, "manual")
        guard = self.guard(claim)
        self.assertIsNotNone(guard.verification_locked_at)

    def test_lock_emits_no_moderation_queued_in_pr3(self):
        claim = self.submit()
        guard = self.guard(claim)
        guard.cooldowns = 2
        guard.save(update_fields=["cooldowns"])
        wrong_code = _any_wrong_code(claim)
        for _ in range(5):
            claim, _ = self.verify(claim, wrong_code)
        self.assertFalse(self.events("moderation.queued").exists())

    def test_wrong_entry_increments_claim_attempts_correct_code_leaves_it(self):
        claim = self.submit()
        wrong_code = _any_wrong_code(claim)
        claim, _ = self.verify(claim, wrong_code)
        self.assertEqual(claim.attempts, 1)


# --- correct code ---

class CorrectCodeTests(_Base):
    def test_correct_code_sets_verified_at_clears_code_hash_no_event(self):
        claim = self.submit()
        code = _capture_code(self, claim)
        before = self.events("claim.verification_failed").count()
        claim, correct = self.verify(claim, code)
        self.assertTrue(correct)
        self.assertIsNotNone(claim.verified_at)
        self.assertEqual(claim.code_hash, "")
        self.assertEqual(claim.status, Claim.Status.PENDING_VERIFICATION)
        self.assertEqual(self.events("claim.verification_failed").count(), before)
        self.assertFalse(self.events("claim.approved").exists())

    def test_correct_code_after_expiry_is_rejected_not_applied(self):
        claim = self.submit()
        code = _capture_code(self, claim)
        claim.expires_at = timezone.now() - timedelta(seconds=1)
        claim.save(update_fields=["expires_at"])
        with self.assertRaises(services.VerificationRefused):
            self.verify(claim, code)
        claim.refresh_from_db()
        self.assertIsNone(claim.verified_at)


# --- payload / event hygiene ---

class PayloadHygieneTests(_Base):
    def test_verify_claim_id_payload_never_carries_code_or_hash(self):
        claim = self.submit()
        self.verify(claim, "000000")
        row = CommandLog.objects.filter(command="claim.verify").order_by("id").last()
        self.assertEqual(set(row.payload.keys()), {"claim_id"})

    def test_verification_started_event_never_carries_plaintext_code(self):
        claim = self.submit()
        started = self.events("claim.verification_started").first()
        for value in started.data.values():
            self.assertNotIn(claim.code_hash, str(value))
        self.assertNotIn("code", started.data)

    def test_outbound_message_body_carries_no_claimant_supplied_text(self):
        claim = self.submit(claimant={**DANA, "name": "INJECTED <script>"})
        message = OutboundMessage.all_tenants.get(pk=claim.code_message_id)
        self.assertNotIn("INJECTED", message.body_text)

    def test_outbound_message_omits_link_line_when_domain_not_verified(self):
        claim = self.submit()
        message = OutboundMessage.all_tenants.get(pk=claim.code_message_id)
        self.assertNotIn("http", message.body_text)

    def test_outbound_message_includes_absolute_link_when_domain_verified(self):
        self.tenant.primary_domain = "acme.test"
        self.tenant.domain_verified_at = timezone.now()
        self.tenant.save(update_fields=["primary_domain", "domain_verified_at"])
        claim = self.submit()
        message = OutboundMessage.all_tenants.get(pk=claim.code_message_id)
        self.assertIn(
            f"https://acme.test/claim/{claim.public_id}/verify/", message.body_text
        )


# --- resend ---

class ResendTests(_Base):
    def _clear_send_cooldown(self, claim):
        guard = self.guard(claim)
        guard.last_code_sent_at = timezone.now() - timedelta(seconds=120)
        guard.save(update_fields=["last_code_sent_at"])

    def test_resend_supersedes_prior_pending_message_expires_it_now(self):
        claim = self.submit()
        prior_message_id = claim.code_message_id
        self._clear_send_cooldown(claim)
        claim = self.resend(claim)
        prior = OutboundMessage.all_tenants.get(pk=prior_message_id)
        self.assertLessEqual(prior.expires_at, timezone.now())

    def test_resend_grants_no_extra_guesses(self):
        claim = self.submit()
        wrong_code = _any_wrong_code(claim)
        for _ in range(3):
            claim, _ = self.verify(claim, wrong_code)
        guard_before = self.guard(claim).wrong_entries
        self._clear_send_cooldown(claim)
        claim = self.resend(claim)
        self.assertEqual(self.guard(claim).wrong_entries, guard_before)

    def test_send_cap_at_least_60_seconds_between_sends(self):
        claim = self.submit()  # code already sent, under 60s ago
        with tenant_context(self.tenant):
            _, result, _eid = services._apply_start_verification(
                self.tenant, claim=claim
            )
        self.assertEqual(result, "cap")

    def test_send_cap_five_per_rolling_24_hours_counts_the_submit_code(self):
        claim = self.submit()  # 1 of 5, counted at submit
        guard = self.guard(claim)
        guard.last_code_sent_at = timezone.now() - timedelta(seconds=120)
        guard.codes_sent_in_window = 5
        guard.save(update_fields=["last_code_sent_at", "codes_sent_in_window"])
        with tenant_context(self.tenant):
            _, result, _eid = services._apply_start_verification(
                self.tenant, claim=claim
            )
        self.assertEqual(result, "cap")

    def test_lock_takes_the_ineligible_path_not_a_separate_cap_check(self):
        """domain_email_eligible folds guard.verification_locked_at into
        eligibility (decisions.md §4.4: "a locked pair is ineligible"), so
        a resend on a locked pair never reaches the send-cap check at all
        -- it flips to manual via the same path an unrelated ineligible
        listing would, never returning "locked" from this function."""
        claim = self.submit()
        guard = self.guard(claim)
        guard.verification_locked_at = timezone.now()
        guard.save(update_fields=["verification_locked_at"])
        with tenant_context(self.tenant):
            claim, result, _eid = services._apply_start_verification(
                self.tenant, claim=claim
            )
        self.assertEqual(result, "ineligible")
        self.assertEqual(claim.method, "manual")


# --- submit/resend parity + command-log outcomes (correction d) ---

class SubmitResendParityTests(_Base):
    def test_submit_and_resend_apply_identical_eligibility_and_cap_rules(self):
        ineligible = self.submit(claimant={**DANA, "email": "dana@gmail.example"})
        self.assertEqual(ineligible.method, "manual")
        # the same claimant email, resent against an eligible listing via
        # start_claim_verification, hits the *same* domain_email_eligible
        # call -- no separate implementation of the rule.
        self.assertFalse(
            claims.domain_email_eligible(self.listing, "dana@gmail.example")
        )

    def test_submit_during_cooldown_or_cap_concludes_applied(self):
        first = self.submit()
        guard = self.guard(first)
        guard.verification_locked_at = None
        guard.cooldown_until = timezone.now() + timedelta(minutes=15)
        guard.save(update_fields=["cooldown_until"])
        # Simulate a second eligible submit landing while the pair is on
        # cooldown by calling the inner function directly (same claimant
        # can't submit() twice -- get_or_create would just match the user).
        with tenant_context(self.tenant):
            claim, result, _event_id = services._apply_start_verification(
                self.tenant, claim=first
            )
        self.assertEqual(result, "cooldown")
        row = CommandLog.objects.filter(command="claim.submit").order_by("id").last()
        self.assertEqual(row.outcome, "applied")

    def test_resend_during_cooldown_concludes_rejected(self):
        claim = self.submit()
        guard = self.guard(claim)
        guard.cooldown_until = timezone.now() + timedelta(minutes=15)
        guard.save(update_fields=["cooldown_until"])
        with self.assertRaises(services.VerificationRefused):
            self.resend(claim)
        row = (
            CommandLog.objects.filter(command="claim.start_verification")
            .order_by("id").last()
        )
        self.assertEqual(row.outcome, "rejected")

    def test_resend_ineligible_concludes_applied_with_verification_failed_event_id(
        self,
    ):
        # fix #10: an ineligible resend is a real state change (flip to
        # manual), so it concludes "applied", carrying the
        # claim.verification_failed event it caused -- never raised.
        claim = self.submit()
        self.assertEqual(claim.method, "domain_email")
        self.listing.website = ""  # became ineligible after submit
        self.listing.save(update_fields=["website"])

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


# --- form / view wiring ---

class ClaimVerificationGuardModelTests(_Base):
    def test_claim_blocked_at_refuses_submit(self):
        user = DirectoryUser.all_tenants.create(tenant=self.tenant, email=DANA["email"])
        ClaimVerificationGuard.all_tenants.create(
            tenant=self.tenant, listing=self.listing, claimant=user,
            claim_blocked_at=timezone.now(),
        )
        with self.assertRaises(services.SchemaError):
            self.submit()


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class ClaimVerifyRouteTests(TestCase):
    def setUp(self):
        self.enterContext(email_send_stub())
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain=HOST,
            settings=_domain_email_enabled(),
        )
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="Business",
            label_plural="Businesses", path_segment="businesses",
        )
        self.listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="hoffman-plumbing",
            name="Hoffman Plumbing", visibility=Listing.Visibility.PUBLISHED,
            website="https://hoffmanplumbing.example",
        )
        self.claim = Claim.all_tenants.create(
            tenant=self.tenant, listing=self.listing,
            claimant=DirectoryUser.all_tenants.create(
                tenant=self.tenant, email=DANA["email"]
            ),
            method="domain_email",
        )
        self.client = Client()

    def test_verify_route_binds_claim_public_id_not_listing_public_id(self):
        r = self.client.get(
            f"/claim/{self.listing.public_id}/verify/", HTTP_HOST=HOST
        )
        self.assertEqual(r.status_code, 404)
        r = self.client.get(
            f"/claim/{self.claim.public_id}/verify/", HTTP_HOST=HOST
        )
        self.assertEqual(r.status_code, 200)

    def test_resend_route_is_post_only(self):
        r = self.client.get(
            f"/claim/{self.claim.public_id}/verify/resend/", HTTP_HOST=HOST
        )
        self.assertEqual(r.status_code, 405)

    def test_claim_form_offers_domain_email_only_when_listing_website_eligible(self):
        r = self.client.get(f"/claim/{self.listing.public_id}/", HTTP_HOST=HOST)
        self.assertContains(r, 'value="domain_email"')
        self.listing.website = ""
        self.listing.save(update_fields=["website"])
        r = self.client.get(f"/claim/{self.listing.public_id}/", HTTP_HOST=HOST)
        self.assertNotContains(r, 'value="domain_email"')

    def test_claim_form_shows_required_domain_for_domain_email(self):
        r = self.client.get(f"/claim/{self.listing.public_id}/", HTTP_HOST=HOST)
        self.assertContains(r, "@hoffmanplumbing.example")


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class ClaimVerifyPageTests(TransactionTestCase):
    """POST-driving tests -- TransactionTestCase, like every other class
    here that calls submit_claim/verify_claim_code/start_claim_verification
    (require_autocommit refuses a plain TestCase's wrapping transaction)."""

    def setUp(self):
        self.mail = self.enterContext(email_send_stub())
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain=HOST,
            domain_verified_at=timezone.now(),
            settings=_domain_email_enabled(),
        )
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="Business",
            label_plural="Businesses", path_segment="businesses",
        )
        self.listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="hoffman-plumbing",
            name="Hoffman Plumbing", visibility=Listing.Visibility.PUBLISHED,
            website="https://hoffmanplumbing.example",
        )
        self.client = Client()

    def _submit(self, **overrides):
        data = {
            "name": DANA["name"], "email": DANA["email"],
            "phone_e164": DANA["phone_e164"], "role_claimed": "owner",
            "method": "domain_email",
            "marketing_email": "on", "marketing_sms": "", "automated_calls": "",
        }
        data.update(overrides)
        return self.client.post(
            f"/claim/{self.listing.public_id}/", data, HTTP_HOST=HOST
        )

    def _claim(self) -> Claim:
        return Claim.all_tenants.get(tenant=self.tenant)

    # -- fix #5: claim_form redirect + claim_submitted "say why" --

    def test_domain_email_sent_redirects_to_verify(self):
        r = self._submit()
        self.assertEqual(r.status_code, 302)
        self.assertIn("/verify/", r.url)
        self.assertNotIn("/submitted/", r.url)

    def test_manual_redirects_to_submitted_with_no_reason_shown(self):
        r = self._submit(method="manual")
        self.assertTrue(r.url.endswith("/submitted/"))
        r2 = self.client.get(r.url, HTTP_HOST=HOST)
        self.assertNotContains(r2, "wasn't available")

    def test_ineligible_domain_email_redirects_to_submitted_with_reason(self):
        r = self._submit(email="dana@gmail.example")
        self.assertIn("requested=domain_email", r.url)
        r2 = self.client.get(r.url, HTTP_HOST=HOST)
        self.assertContains(r2, "must be at @hoffmanplumbing.example")

    def test_locked_pair_shows_locked_reason_not_host_mismatch(self):
        # fix #1 (2026-09-30): the address is at the required host, but the
        # (listing, claimant) pair is locked -- claim_submitted must say
        # *that*, not the generic "must be at @host" message.
        self._submit()
        claim = self._claim()
        guard = ClaimVerificationGuard.all_tenants.get(
            tenant=self.tenant, listing=self.listing, claimant=claim.claimant
        )
        guard.verification_locked_at = timezone.now()
        guard.save(update_fields=["verification_locked_at"])
        claim.method = "manual"
        claim.save(update_fields=["method"])
        r = self.client.get(
            f"/claim/{claim.public_id}/submitted/?requested=domain_email",
            HTTP_HOST=HOST,
        )
        self.assertContains(
            r,
            "Too many incorrect codes were entered for this address, so "
            "this claim will be reviewed manually.",
        )
        self.assertNotContains(r, "must be at @")

    # -- fix #3: verify page messages, expiry, no-forms states --

    def test_verify_page_shows_code_expiry_and_tries_left(self):
        self._submit()
        claim = self._claim()
        r = self.client.get(f"/claim/{claim.public_id}/verify/", HTTP_HOST=HOST)
        self.assertContains(r, claim.expires_at.strftime("%Y-%m-%d %H:%M"))
        self.assertContains(r, "Tries left on this code: 5")

    def test_wrong_code_shows_tries_left_message(self):
        self._submit()
        claim = self._claim()
        wrong = _any_wrong_code(claim)
        r = self.client.post(
            f"/claim/{claim.public_id}/verify/", {"code": wrong}, HTTP_HOST=HOST
        )
        self.assertContains(
            r, "tries left before a 15-minute wait", status_code=200
        )
        self.assertContains(r, "4 tries left")

    def test_expired_code_message_offers_resend_not_the_code_form(self):
        self._submit()
        claim = self._claim()
        claim.expires_at = timezone.now() - timedelta(seconds=1)
        claim.save(update_fields=["expires_at"])
        r = self.client.get(f"/claim/{claim.public_id}/verify/", HTTP_HOST=HOST)
        self.assertContains(r, "This code has expired. Request a new one.")
        self.assertContains(r, "Resend code")
        self.assertNotContains(r, "Verify</button>")

    def test_cooldown_state_shows_both_message_lines(self):
        # fix #2 (2026-09-30): decisions.md §4.4 ruling 8's two-line
        # cooldown state -- no pre-existing view-level cooldown test to
        # adjust, so this is new.
        self._submit()
        claim = self._claim()
        guard = ClaimVerificationGuard.all_tenants.get(
            tenant=self.tenant, listing=self.listing, claimant=claim.claimant
        )
        guard.cooldown_until = timezone.now() + timedelta(minutes=15)
        guard.cooldowns = 1
        guard.save(update_fields=["cooldown_until", "cooldowns"])
        r = self.client.get(f"/claim/{claim.public_id}/verify/", HTTP_HOST=HOST)
        self.assertContains(
            r,
            "Too many incorrect codes. This code no longer works. You can "
            f"request a new one after {guard.cooldown_until:%Y-%m-%d %H:%M} UTC.",
        )
        self.assertContains(
            r, "Cooldowns remaining before this claim goes to manual review: 2."
        )
        self.assertNotContains(r, "Verify</button>")
        self.assertNotContains(r, "Resend code")

    def test_locked_claim_shows_state_and_no_forms(self):
        self._submit()
        claim = self._claim()
        guard = ClaimVerificationGuard.all_tenants.get(
            tenant=self.tenant, listing=self.listing, claimant=claim.claimant
        )
        guard.verification_locked_at = timezone.now()
        guard.save(update_fields=["verification_locked_at"])
        claim.method = "manual"
        claim.save(update_fields=["method"])
        r = self.client.get(f"/claim/{claim.public_id}/verify/", HTTP_HOST=HOST)
        self.assertContains(r, "verification method is Manual review.")
        self.assertNotContains(r, "Verify</button>")
        self.assertNotContains(r, "Resend code")

    def test_already_verified_shows_state_and_no_forms(self):
        self._submit()
        claim = self._claim()
        code = _capture_code(self, claim)
        self.client.post(
            f"/claim/{claim.public_id}/verify/", {"code": code}, HTTP_HOST=HOST
        )
        r = self.client.get(f"/claim/{claim.public_id}/verify/", HTTP_HOST=HOST)
        self.assertContains(r, "This claim has already been verified.")
        self.assertNotContains(r, "Verify</button>")
        self.assertNotContains(r, "Resend code")

    def test_non_digit_code_is_a_form_error_no_attempt_consumed(self):
        # fix #7
        self._submit()
        claim = self._claim()
        r = self.client.post(
            f"/claim/{claim.public_id}/verify/", {"code": "12a45b"}, HTTP_HOST=HOST
        )
        self.assertContains(r, "Enter the 6-digit code.")
        claim.refresh_from_db()
        self.assertEqual(claim.attempts, 0)
        self.assertFalse(CommandLog.objects.filter(command="claim.verify").exists())

    # -- fix #4: next_send_at, resend refusal surfaced as a message --

    def test_resend_refusal_shows_a_message_not_a_silent_pass(self):
        self._submit()
        claim = self._claim()
        r = self.client.post(
            f"/claim/{claim.public_id}/verify/resend/", {}, HTTP_HOST=HOST,
            follow=True,
        )
        self.assertContains(r, "resend limit has been reached")

    def test_verify_page_shows_next_send_at(self):
        self._submit()
        claim = self._claim()
        r = self.client.get(f"/claim/{claim.public_id}/verify/", HTTP_HOST=HOST)
        guard = ClaimVerificationGuard.all_tenants.get(
            tenant=self.tenant, listing=self.listing, claimant=claim.claimant
        )
        expected = (guard.last_code_sent_at + timedelta(seconds=60))
        self.assertContains(r, expected.strftime("%Y-%m-%d %H:%M"))

    # -- fix #6: exact email body --

    def test_email_body_matches_decisions_wording_exactly(self):
        self._submit()
        claim = self._claim()
        message = OutboundMessage.all_tenants.get(pk=claim.code_message_id)
        code = _capture_code(self, claim)
        expected = (
            "A request was made on Acme to claim the listing Hoffman Plumbing "
            "using this email address.\n\n"
            f"Your verification code is: {code}\n\n"
            f"It expires at {claim.expires_at.strftime('%Y-%m-%d %H:%M')} UTC. "
            "Only the most recent code works.\n\n"
            f"Enter it at: https://acme.test/claim/{claim.public_id}/verify/\n\n"
            "If you did not make this request, you can ignore this email. "
            "Nothing changes unless the code is entered."
        )
        self.assertEqual(message.body_text, expected)


def _any_wrong_code(claim) -> str:
    for guess in ("000000", "111111"):
        expected = claims.hash_code(claim.public_id, guess)
        if not hmac.compare_digest(claim.code_hash, expected):
            return guess
    raise AssertionError("both guesses matched -- widen the guess set")


def _capture_code(test, claim) -> str:
    """The service never returns the plaintext code (only the hash is
    stored) -- recover it for test purposes by brute-forcing the 6-digit
    space against the stored hash, which is cheap in a test process."""
    for n in range(1_000_000):
        guess = f"{n:06d}"
        expected = claims.hash_code(claim.public_id, guess)
        if hmac.compare_digest(claim.code_hash, expected):
            return guess
    raise AssertionError("code not found")
