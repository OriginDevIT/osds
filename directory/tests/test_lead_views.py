"""The public lead form (decisions.md §4.10): where it appears, the two traps,
consent, the 429, and what a trapped or refused submission does and does not
tell the visitor.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import Client, override_settings
from django.utils import timezone

from audit.models import CommandLog
from audit.ratelimit import Rule
from directory import lead_limits, lead_views, services
from directory.models import Consent, Lead
from directory.tests.lead_base import GOOD_MESSAGE, HOST, LeadBase
from tenants.models import Tenant


def one_per_minute(name):
    return (Rule(name, 1, timedelta(minutes=1)),)


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class _ViewBase(LeadBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.url = f"/lead/{self.listing.public_id}/"

    def get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=HOST, **kw)

    def post(self, data, **extra):
        return self.client.post(self.url, data, HTTP_HOST=HOST, **extra)

    def token(self, seconds_ago=30):
        return lead_views.issue_token(timezone.now() - timedelta(seconds=seconds_ago))

    def data(self, **over):
        self.n += 1
        data = {
            "name": "Priya R.",
            "email": f"priya{self.n}@example.test",
            "phone_e164": "",
            "message": f"{GOOD_MESSAGE} ({self.n})",
            "contact_by_business": "on",
            "company_url": "",
            "form_token": self.token(),
        }
        data.update(over)
        return data

    def disable(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(settings={})


class FormPageTests(_ViewBase):
    def test_renders_with_the_wording_a_token_and_the_trap(self):
        r = self.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "may contact you about your inquiry")
        self.assertContains(r, 'name="form_token"')
        self.assertContains(r, 'name="company_url"')
        self.assertEqual(r["X-Robots-Tag"], "noindex, nofollow")
        self.assertIn("no-cache", r["Cache-Control"])
        self.assertContains(r, "I agree that Hoffman-Plumbing may contact me")

    def test_the_consent_box_starts_unchecked(self):
        r = self.get(self.url)
        self.assertNotContains(r, "checked")

    def test_a_disabled_directory_exposes_nothing(self):
        self.disable()
        self.assertEqual(self.get(self.url).status_code, 404)
        self.assertEqual(self.get(self.url + "sent/").status_code, 404)

    def test_an_unpublished_or_unknown_listing_is_a_404(self):
        self.listing.visibility = "draft"
        self.listing.save(update_fields=["visibility"])
        self.assertEqual(self.get(self.url).status_code, 404)
        self.assertEqual(self.get("/lead/listing_nope/").status_code, 404)

    def test_the_listing_page_links_to_the_form_only_when_enabled(self):
        page = self.get("/plumbers/hoffman-plumbing")
        self.assertContains(page, f'href="{self.url}"')
        self.disable()
        self.assertNotContains(self.get("/plumbers/hoffman-plumbing"), "Send an inquiry")

    def test_lead_is_a_reserved_slug(self):
        self.assertIn("lead", services.RESERVED_SLUGS)


class SubmitTests(_ViewBase):
    def test_a_valid_submission_creates_the_lead_and_redirects_to_the_sent_page(self):
        r = self.post(self.data())
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, self.url + "sent/")
        lead = Lead.all_tenants.get()
        self.assertEqual(lead.listing_id, self.listing.pk)
        self.assertEqual(lead.source_page, "/plumbers/hoffman-plumbing")
        self.assertEqual(Consent.all_tenants.get(lead=lead).ip, "127.0.0.1")
        sent = self.get(r.url)
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(sent["X-Robots-Tag"], "noindex, nofollow")
        self.assertContains(sent, "we will not")

    def test_the_source_page_is_the_servers_not_the_visitors(self):
        self.post({**self.data(), "source_page": "https://evil.example/"})
        self.assertEqual(Lead.all_tenants.get().source_page, "/plumbers/hoffman-plumbing")

    def test_unchecked_consent_is_refused_on_the_field_and_logged(self):
        r = self.post(self.data(contact_by_business=""))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "You need to agree to be contacted")
        self.assertFalse(Lead.all_tenants.exists())
        self.assertEqual(
            CommandLog.objects.get(command="lead.create").problem,
            {"missing_consent": "contact_by_business"},
        )

    def test_field_errors_re_render_without_calling_the_command(self):
        r = self.post(self.data(message="short", email="not-an-email"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(CommandLog.objects.filter(command="lead.create").exists())
        self.assertContains(r, "at least")

    def test_a_malformed_phone_is_a_form_error(self):
        r = self.post(self.data(phone_e164="12"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Lead.all_tenants.exists())

    def test_every_render_gets_a_fresh_token(self):
        first = self.get(self.url).content.decode()
        self.assertIn("form_token", first)
        r = self.post(self.data(message="short"))
        self.assertContains(r, 'name="form_token"')


class TrapTests(_ViewBase):
    def test_a_filled_honeypot_looks_like_success_and_creates_nothing(self):
        r = self.post(self.data(company_url="http://bot.example"))
        self.assertEqual((r.status_code, r.url), (302, self.url + "sent/"))
        self.assertFalse(Lead.all_tenants.exists())
        self.assertFalse(CommandLog.objects.filter(command="lead.create").exists())

    def test_a_form_sent_faster_than_a_person_looks_like_success_and_creates_nothing(self):
        r = self.post(self.data(form_token=self.token(seconds_ago=0)))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Lead.all_tenants.exists())

    def test_a_missing_or_forged_token_is_a_form_error_not_a_trap(self):
        for token in ("", "garbage", "123:forged"):
            with self.subTest(token=token):
                r = self.post(self.data(form_token=token))
                self.assertEqual(r.status_code, 200)
                self.assertContains(r, "This form has expired")
        self.assertFalse(Lead.all_tenants.exists())

    def test_a_stale_token_is_expired(self):
        r = self.post(self.data(form_token=self.token(seconds_ago=3 * 3600)))
        self.assertContains(r, "This form has expired")
        self.assertFalse(Lead.all_tenants.exists())

    def test_a_bot_that_keeps_coming_is_refused(self):
        with mock.patch.object(lead_limits, "SPAM_TRAP_IP", one_per_minute("lead.spam_trap.ip.1h")):
            first = self.post(self.data(company_url="x"))
            second = self.post(self.data(company_url="x"))
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 429)
        self.assertTrue(int(second["Retry-After"]) > 0)

    def test_a_real_submission_is_not_trapped_by_the_age_of_its_token(self):
        for seconds in (4, 60, 3600):
            with self.subTest(seconds=seconds):
                Lead.all_tenants.all().delete()
                r = self.post(self.data(form_token=self.token(seconds_ago=seconds)))
                self.assertEqual(r.status_code, 302)
                self.assertTrue(Lead.all_tenants.exists())


class LimitTests(_ViewBase):
    def test_a_refused_submit_is_a_429_with_retry_after_and_keeps_the_form(self):
        with mock.patch.object(lead_limits, "CREATE_IP", one_per_minute("lead.create.ip.10m")):
            first = self.post(self.data())
            second = self.post(self.data(email="second@example.test"))
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 429)
        self.assertTrue(1 <= int(second["Retry-After"]) <= 60)
        self.assertContains(second, "Too many attempts", status_code=429)
        self.assertContains(second, "second@example.test", status_code=429)
        self.assertNotContains(second, "lead.create", status_code=429)
        self.assertEqual(Lead.all_tenants.count(), 1)

    def test_a_get_is_never_limited(self):
        with mock.patch.object(lead_limits, "CREATE_IP", one_per_minute("lead.create.ip.10m")):
            self.post(self.data())
            self.post(self.data())
            self.assertEqual(self.get(self.url).status_code, 200)

    @override_settings(OSDS_TRUSTED_PROXIES=("127.0.0.1/32",))
    def test_behind_a_trusted_proxy_each_client_has_its_own_budget(self):
        with mock.patch.object(lead_limits, "CREATE_IP", one_per_minute("lead.create.ip.10m")):
            statuses = [
                self.post(self.data(), HTTP_X_FORWARDED_FOR=client).status_code
                for client in ("198.51.100.1", "198.51.100.2", "198.51.100.1")
            ]
        self.assertEqual(statuses, [302, 302, 429])

    @override_settings(OSDS_TRUSTED_PROXIES=("127.0.0.1/32",))
    def test_the_consent_record_keeps_the_client_address_behind_a_trusted_proxy(self):
        self.post(self.data(), HTTP_X_FORWARDED_FOR="198.51.100.23")
        self.assertEqual(Consent.all_tenants.get().ip, "198.51.100.23")

    def test_a_forged_forwarded_header_does_not_escape_the_limit(self):
        with mock.patch.object(lead_limits, "CREATE_IP", one_per_minute("lead.create.ip.10m")):
            a = self.post(self.data(), HTTP_X_FORWARDED_FOR="1.1.1.1")
            b = self.post(self.data(), HTTP_X_FORWARDED_FOR="2.2.2.2")
        self.assertEqual((a.status_code, b.status_code), (302, 429))
