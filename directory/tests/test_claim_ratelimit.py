"""The claim-flow rate limits (spec §9.4, #210, decisions.md §4.8): the three
commands that enforce them, and the views that turn a refusal into a 429.

Every command refuses to run inside an open transaction, so these are
TransactionTestCases. Where a test needs a small limit it patches the constant
in ``directory.claim_limits``; the shipped numbers are asserted separately.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from audit.models import CommandLog, OutboundMessage, OutboxEvent, RateLimitCounter
from audit.ratelimit import RateLimited, Rule
from directory import claim_limits, services
from directory.models import (
    Claim,
    ClaimVerificationGuard,
    Consent,
    DirectoryUser,
    Listing,
    ListingType,
)
from osds.tenancy import tenant_context
from osds.tests.mail_stub import email_send_stub
from tenants.models import InstallSetup, Tenant

HOST = "acme.test"
IP = "203.0.113.44"

GRANTED_ALL = {
    "marketing_email": {"granted": True},
    "marketing_sms": {"granted": True},
    "automated_calls": {"granted": False},
}


def one_per_minute(name):
    return (Rule(name, 1, timedelta(minutes=1)),)


class _Base(TransactionTestCase):
    def setUp(self):
        self.mail = self.enterContext(email_send_stub())
        self.tenant = Tenant.objects.create(
            slug="acme",
            name="Acme",
            primary_domain=HOST,
            settings={"claim_verification": {"enabled_methods": ["manual", "domain_email"]}},
        )
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )
        self.n = 0

    def listing(self, tenant=None, **kw):
        self.n += 1
        tenant = tenant or self.tenant
        lt = self.lt if tenant is self.tenant else ListingType.all_tenants.get(tenant=tenant)
        defaults = dict(
            tenant=tenant, listing_type=lt, slug=f"biz-{self.n}", name=f"Biz {self.n}",
            website="https://hoffmanplumbing.example",
            visibility=Listing.Visibility.PUBLISHED,
        )
        defaults.update(kw)
        return Listing.all_tenants.create(**defaults)

    def claimant(self, email=None):
        self.n += 1
        return {
            "name": "Dana",
            "email": email or f"dana{self.n}@hoffmanplumbing.example",
            "phone_e164": "",
            "role_claimed": "owner",
        }

    def submit(self, *, listing=None, ip=IP, method="manual", tenant=None, email=None):
        tenant = tenant or self.tenant
        with tenant_context(tenant):
            return services.submit_claim(
                tenant,
                listing=listing or self.listing(tenant),
                method=method,
                claimant=self.claimant(email),
                consent=GRANTED_ALL,
                ip=ip,
            )

    def blocked_rows(self, command):
        return CommandLog.objects.filter(command=command, outcome="blocked")


class SubmitIpLimitTests(_Base):
    def test_the_shipped_numbers(self):
        self.assertEqual(
            [(r.limit, r.window) for r in claim_limits.SUBMIT_IP],
            [(5, timedelta(minutes=10)), (20, timedelta(hours=24))],
        )
        self.assertEqual(
            [(r.limit, r.window) for r in claim_limits.SUBMIT_EMAIL],
            [(5, timedelta(hours=24))],
        )
        self.assertEqual(
            [(r.limit, r.window) for r in claim_limits.VERIFY_IP],
            [(30, timedelta(minutes=15))],
        )
        self.assertEqual(
            [(r.limit, r.window) for r in claim_limits.RESEND_IP],
            [(10, timedelta(hours=1))],
        )
        self.assertEqual(
            [(r.limit, r.window) for r in claim_limits.RESEND_EMAIL],
            [(10, timedelta(hours=24))],
        )

    def test_the_sixth_submit_in_ten_minutes_from_one_ip_is_refused(self):
        for _ in range(5):
            self.submit()
        with self.assertRaises(RateLimited) as cm:
            self.submit()
        self.assertEqual(cm.exception.rule, "claim.submit.ip.10m")
        self.assertGreater(cm.exception.retry_after, 0)
        self.assertLessEqual(cm.exception.retry_after, 600)
        self.assertEqual(Claim.all_tenants.count(), 5)

    def test_a_blocked_submit_creates_nothing(self):
        for _ in range(5):
            self.submit()
        events = OutboxEvent.all_tenants.count()
        messages = OutboundMessage.all_tenants.count()
        consents = Consent.all_tenants.count()
        with self.assertRaises(RateLimited):
            self.submit(email="blocked@hoffmanplumbing.example")
        self.assertEqual(OutboxEvent.all_tenants.count(), events)
        self.assertEqual(OutboundMessage.all_tenants.count(), messages)
        self.assertEqual(Consent.all_tenants.count(), consents)
        self.assertFalse(
            DirectoryUser.all_tenants.filter(email="blocked@hoffmanplumbing.example").exists()
        )

    def test_a_flood_writes_one_blocked_log_row_per_window(self):
        for _ in range(5):
            self.submit()
        for _ in range(4):
            with self.assertRaises(RateLimited):
                self.submit()
        rows = self.blocked_rows("claim.submit")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().problem, {"rate_limited": "claim.submit.ip.10m"})
        self.assertIsNotNone(rows.get().concluded_at)

    def test_the_blocked_row_is_the_only_trace_in_the_log(self):
        for _ in range(5):
            self.submit()
        applied = CommandLog.objects.filter(command="claim.submit", outcome="applied").count()
        with self.assertRaises(RateLimited):
            self.submit()
        self.assertEqual(
            CommandLog.objects.filter(command="claim.submit", outcome="applied").count(),
            applied,
        )

    def test_another_ip_is_unaffected(self):
        for _ in range(5):
            self.submit()
        self.assertIsNotNone(self.submit(ip="198.51.100.9"))

    def test_an_ipv6_neighbour_in_the_same_64_shares_the_budget(self):
        for i in range(5):
            self.submit(ip=f"2001:db8:1:2::{i + 1}")
        with self.assertRaises(RateLimited):
            self.submit(ip="2001:db8:1:2:ffff::1")

    def test_no_ip_means_no_ip_limit(self):
        for _ in range(7):
            self.submit(ip=None)
        self.assertEqual(Claim.all_tenants.count(), 7)

    def test_the_budget_belongs_to_one_tenant(self):
        other = Tenant.objects.create(slug="other", name="Other", primary_domain="other.test")
        ListingType.all_tenants.create(
            tenant=other, key="business", label_singular="B", label_plural="Bs",
            path_segment="businesses",
        )
        for _ in range(5):
            self.submit()
        self.assertIsNotNone(self.submit(tenant=other))

    def test_a_new_window_allows_the_ip_again(self):
        for _ in range(5):
            self.submit()
        later = timezone.now() + timedelta(minutes=11)
        with mock.patch("django.utils.timezone.now", return_value=later):
            self.assertIsNotNone(self.submit())

    def test_the_count_survives_a_command_that_fails_mid_apply(self):
        with mock.patch.object(services, "_apply_submit_claim", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                self.submit()
        self.assertEqual(RateLimitCounter.all_tenants.filter(rule="claim.submit.ip.10m").get().count, 1)

    def test_the_daily_rule_also_applies(self):
        with mock.patch.object(
            claim_limits, "SUBMIT_IP", (Rule("claim.submit.ip.24h", 2, timedelta(hours=24)),)
        ):
            self.submit()
            self.submit()
            with self.assertRaises(RateLimited) as cm:
                self.submit()
        self.assertEqual(cm.exception.rule, "claim.submit.ip.24h")


class SubmitEmailLimitTests(_Base):
    def test_the_sixth_claim_by_one_email_across_listings_is_refused(self):
        email = "dana@hoffmanplumbing.example"
        for i in range(5):
            self.submit(ip=f"198.51.100.{i + 1}", email=email)
        with self.assertRaises(RateLimited) as cm:
            self.submit(ip="198.51.100.99", email=email)
        self.assertEqual(cm.exception.rule, "claim.submit.email.24h")
        self.assertEqual(self.blocked_rows("claim.submit").count(), 1)

    def test_email_matching_ignores_case(self):
        for i in range(5):
            self.submit(ip=f"198.51.100.{i + 1}", email="Dana@HoffmanPlumbing.example")
        with self.assertRaises(RateLimited):
            self.submit(ip="198.51.100.99", email="dana@hoffmanplumbing.example")

    def test_a_request_refused_on_its_ip_does_not_spend_the_email_budget(self):
        email = "victim@hoffmanplumbing.example"
        for _ in range(5):
            self.submit()  # exhaust the IP
        for _ in range(3):
            with self.assertRaises(RateLimited):
                self.submit(email=email)
        self.assertFalse(
            RateLimitCounter.all_tenants.filter(rule="claim.submit.email.24h", count__gt=5).exists()
        )
        # The victim, from another address, still has their whole allowance.
        for i in range(5):
            self.submit(ip=f"198.51.100.{i + 1}", email=email)

    def test_a_malformed_submission_is_rejected_before_any_counter_moves(self):
        with self.assertRaises(Exception):
            with tenant_context(self.tenant):
                services.submit_claim(
                    self.tenant, listing=self.listing(), method="manual",
                    claimant={"name": "x", "email": "", "phone_e164": "", "role_claimed": "owner"},
                    consent=GRANTED_ALL, ip=IP,
                )
        self.assertFalse(RateLimitCounter.all_tenants.exists())


class VerifyAndResendLimitTests(_Base):
    def claim(self):
        return self.submit(method="domain_email", ip=None)

    def test_the_verify_ip_limit_refuses_without_touching_the_guard(self):
        claim = self.claim()
        with mock.patch.object(claim_limits, "VERIFY_IP", (Rule("claim.verify.ip.15m", 2, timedelta(minutes=15)),)):
            for _ in range(2):
                with tenant_context(self.tenant):
                    services.verify_claim_code(self.tenant, claim=claim, code="000000", ip=IP)
            with self.assertRaises(RateLimited) as cm:
                with tenant_context(self.tenant):
                    services.verify_claim_code(self.tenant, claim=claim, code="000000", ip=IP)
        self.assertEqual(cm.exception.rule, "claim.verify.ip.15m")
        guard = ClaimVerificationGuard.all_tenants.get(claimant=claim.claimant)
        self.assertEqual(guard.wrong_entries, 2)  # the refused attempt was not counted
        self.assertEqual(self.blocked_rows("claim.verify").count(), 1)

    def test_verify_without_an_ip_is_not_ip_limited(self):
        claim = self.claim()
        with mock.patch.object(claim_limits, "VERIFY_IP", one_per_minute("claim.verify.ip.15m")):
            for _ in range(3):
                with tenant_context(self.tenant):
                    services.verify_claim_code(self.tenant, claim=claim, code="000000", ip=None)

    def test_the_resend_ip_limit_refuses_ahead_of_the_per_pair_guard(self):
        claim = self.claim()
        with mock.patch.object(claim_limits, "RESEND_IP", one_per_minute("claim.resend.ip.1h")):
            # First: the limiter lets it through and the 60-second floor refuses it.
            with self.assertRaises(services.VerificationRefused):
                with tenant_context(self.tenant):
                    services.start_claim_verification(self.tenant, claim=claim, ip=IP)
            # Second: the limiter refuses before the guard is consulted.
            with self.assertRaises(RateLimited) as cm:
                with tenant_context(self.tenant):
                    services.start_claim_verification(self.tenant, claim=claim, ip=IP)
        self.assertEqual(cm.exception.rule, "claim.resend.ip.1h")
        self.assertEqual(self.blocked_rows("claim.start_verification").count(), 1)

    def test_the_resend_email_limit_protects_the_recipient_across_ips(self):
        claim = self.claim()
        with mock.patch.object(claim_limits, "RESEND_EMAIL", one_per_minute("claim.resend.email.24h")):
            with self.assertRaises(services.VerificationRefused):
                with tenant_context(self.tenant):
                    services.start_claim_verification(self.tenant, claim=claim, ip="198.51.100.1")
            with self.assertRaises(RateLimited) as cm:
                with tenant_context(self.tenant):
                    services.start_claim_verification(self.tenant, claim=claim, ip="198.51.100.2")
        self.assertEqual(cm.exception.rule, "claim.resend.email.24h")

    def test_the_nested_start_inside_submit_is_not_counted_as_a_resend(self):
        self.claim()
        self.assertFalse(
            RateLimitCounter.all_tenants.filter(rule__startswith="claim.resend").exists()
        )


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class ViewTests(_Base):
    def setUp(self):
        super().setUp()
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.client = Client()

    def post(self, path, data, **extra):
        return self.client.post(path, data, HTTP_HOST=HOST, **extra)

    def form(self, listing, email):
        return self.post(
            f"/claim/{listing.public_id}/",
            {
                "name": "Dana Hoffman", "email": email, "phone_e164": "",
                "role_claimed": "owner", "method": "manual", "marketing_email": "on",
            },
        )

    def test_a_refused_submit_is_a_429_with_retry_after_and_keeps_the_form(self):
        with mock.patch.object(claim_limits, "SUBMIT_IP", one_per_minute("claim.submit.ip.10m")):
            first = self.form(self.listing(), "a@hoffmanplumbing.example")
            second = self.form(self.listing(), "b@hoffmanplumbing.example")
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 429)
        self.assertTrue(1 <= int(second["Retry-After"]) <= 60)
        self.assertContains(second, "Too many attempts", status_code=429)
        self.assertContains(second, "b@hoffmanplumbing.example", status_code=429)
        self.assertEqual(Claim.all_tenants.count(), 1)

    def test_the_message_states_the_wait_and_not_the_rule(self):
        with mock.patch.object(claim_limits, "SUBMIT_IP", one_per_minute("claim.submit.ip.10m")):
            self.form(self.listing(), "a@hoffmanplumbing.example")
            r = self.form(self.listing(), "b@hoffmanplumbing.example")
        self.assertContains(r, "try again in a minute", status_code=429)
        self.assertNotContains(r, "claim.submit", status_code=429)

    def test_a_get_is_never_limited(self):
        listing = self.listing()
        with mock.patch.object(claim_limits, "SUBMIT_IP", one_per_minute("claim.submit.ip.10m")):
            self.form(self.listing(), "a@hoffmanplumbing.example")
            self.form(self.listing(), "b@hoffmanplumbing.example")
            r = self.client.get(f"/claim/{listing.public_id}/", HTTP_HOST=HOST)
        self.assertEqual(r.status_code, 200)

    def test_a_forged_x_forwarded_for_does_not_escape_the_limit(self):
        with mock.patch.object(claim_limits, "SUBMIT_IP", one_per_minute("claim.submit.ip.10m")):
            first = self.post(
                f"/claim/{self.listing().public_id}/",
                self._data("a@hoffmanplumbing.example"),
                HTTP_X_FORWARDED_FOR="1.1.1.1",
            )
            second = self.post(
                f"/claim/{self.listing().public_id}/",
                self._data("b@hoffmanplumbing.example"),
                HTTP_X_FORWARDED_FOR="2.2.2.2",
            )
        self.assertEqual((first.status_code, second.status_code), (302, 429))

    @override_settings(OSDS_TRUSTED_PROXIES=("127.0.0.1/32",))
    def test_behind_a_trusted_proxy_each_client_has_its_own_budget(self):
        with mock.patch.object(claim_limits, "SUBMIT_IP", one_per_minute("claim.submit.ip.10m")):
            statuses = [
                self.post(
                    f"/claim/{self.listing().public_id}/",
                    self._data(f"{i}@hoffmanplumbing.example"),
                    HTTP_X_FORWARDED_FOR=client,
                ).status_code
                for i, client in enumerate(["198.51.100.1", "198.51.100.2", "198.51.100.1"])
            ]
        self.assertEqual(statuses, [302, 302, 429])

    def _data(self, email):
        return {
            "name": "Dana Hoffman", "email": email, "phone_e164": "",
            "role_claimed": "owner", "method": "manual", "marketing_email": "on",
        }

    def test_the_consent_record_keeps_the_peer_address_by_default(self):
        self.post(
            f"/claim/{self.listing().public_id}/",
            self._data("a@hoffmanplumbing.example"),
            HTTP_X_FORWARDED_FOR="198.51.100.23",
        )
        self.assertEqual(Consent.all_tenants.get(channel="marketing_email").ip, "127.0.0.1")

    @override_settings(OSDS_TRUSTED_PROXIES=("127.0.0.1/32",))
    def test_the_consent_record_keeps_the_client_address_behind_a_trusted_proxy(self):
        self.post(
            f"/claim/{self.listing().public_id}/",
            self._data("a@hoffmanplumbing.example"),
            HTTP_X_FORWARDED_FOR="198.51.100.23",
        )
        self.assertEqual(Consent.all_tenants.get(channel="marketing_email").ip, "198.51.100.23")

    def test_a_refused_code_entry_is_a_429(self):
        claim = self.submit(method="domain_email", ip=None)
        with mock.patch.object(claim_limits, "VERIFY_IP", one_per_minute("claim.verify.ip.15m")):
            first = self.post(f"/claim/{claim.public_id}/verify/", {"code": "000000"})
            second = self.post(f"/claim/{claim.public_id}/verify/", {"code": "000000"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertTrue(int(second["Retry-After"]) > 0)
        self.assertContains(second, "Too many attempts", status_code=429)

    def test_a_refused_resend_is_a_429_on_the_verify_page(self):
        claim = self.submit(method="domain_email", ip=None)
        with mock.patch.object(claim_limits, "RESEND_IP", one_per_minute("claim.resend.ip.1h")):
            first = self.post(f"/claim/{claim.public_id}/verify/resend/", {})
            second = self.post(f"/claim/{claim.public_id}/verify/resend/", {})
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 429)
        self.assertTrue(int(second["Retry-After"]) > 0)
        self.assertContains(second, "Too many attempts", status_code=429)
