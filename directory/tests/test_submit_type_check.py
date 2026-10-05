"""Wrongly-typed claimant, contact and consent values (#257).

A non-string email or a non-dict consent used to raise ``AttributeError`` from
inside normalisation or apply, so the request 500ed and the received
``CommandLog`` row was never concluded. Each bad type must now conclude the row
as ``rejected`` (the malformed outcome) with a field-named message, raise
``SchemaError``, and never echo the submitted value.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from audit.models import CommandLog
from audit.tests.window_clock import pinned_windows
from directory import claim_views, lead_views, services
from directory.field_schema import SchemaError
from directory.models import Claim, Lead, Listing, ListingType
from directory.tests.lead_base import GOOD_MESSAGE, HOST, LeadBase
from osds.tenancy import tenant_context
from osds.tests.mail_stub import email_send_stub
from tenants.models import InstallSetup, Tenant

GRANTED_ALL = {
    "marketing_email": {"granted": True},
    "marketing_sms": {"granted": True},
    "automated_calls": {"granted": False},
}
LEAD_GRANTED = {"contact_by_business": {"granted": True}}

# Values that are not text. The marker string only ever appears nested inside
# one, so a leak into the row is detectable.
LEAK = "leak-marker-7431"
NOT_TEXT = [123, 1.5, True, ["a@example.test"], {"k": LEAK}]
GOOD = object()  # "use the default claimant", distinct from a bad None


def row_text(row) -> str:
    return str(CommandLog.objects.filter(pk=row.pk).values().get()).lower()


class ClaimSubmitTypeTests(TransactionTestCase):
    def setUp(self):
        self.enterContext(pinned_windows())
        self.enterContext(email_send_stub())
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")
        lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )
        self.listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug="hoffman-plumbing",
            name="Hoffman Plumbing",
        )

    def claimant(self, **over):
        base = {
            "name": "Dana Hoffman", "email": "dana@hoffmanplumbing.example",
            "phone_e164": "+17735550142", "role_claimed": "owner",
        }
        base.update(over)
        return base

    def assertMalformed(self, field, claimant=GOOD, consent=GRANTED_ALL):
        with self.assertRaises(SchemaError) as cm:
            with tenant_context(self.tenant):
                services.submit_claim(
                    self.tenant, listing=self.listing, method="manual",
                    claimant=self.claimant() if claimant is GOOD else claimant,
                    consent=consent, ip="203.0.113.44",
                )
        row = CommandLog.objects.get(command="claim.submit")
        self.assertEqual(row.outcome, "rejected")
        self.assertIn(field, " ".join(cm.exception.errors))
        self.assertIn(field, str(row.problem))
        self.assertNotIn(LEAK, row_text(row))
        self.assertFalse(Claim.all_tenants.exists())

    def test_a_non_text_email_is_malformed(self):
        for value in NOT_TEXT:
            with self.subTest(value=value):
                CommandLog.objects.all().delete()
                self.assertMalformed("email", self.claimant(email=value))

    def test_a_non_text_name_is_malformed(self):
        for value in NOT_TEXT:
            with self.subTest(value=value):
                CommandLog.objects.all().delete()
                self.assertMalformed("name", self.claimant(name=value))

    def test_a_non_text_phone_is_malformed(self):
        for value in NOT_TEXT:
            with self.subTest(value=value):
                CommandLog.objects.all().delete()
                self.assertMalformed("phone", self.claimant(phone_e164=value))

    def test_a_non_text_role_is_malformed(self):
        for value in NOT_TEXT:
            with self.subTest(value=value):
                CommandLog.objects.all().delete()
                self.assertMalformed("role", self.claimant(role_claimed=value))

    def test_a_non_dict_claimant_is_malformed(self):
        for value in ("dana", 5, ["x"], None):
            with self.subTest(value=value):
                CommandLog.objects.all().delete()
                self.assertMalformed("claimant", claimant=value)

    def test_a_non_dict_consent_is_malformed(self):
        for value in ("marketing_email", 5, ["marketing_email"], None):
            with self.subTest(value=value):
                CommandLog.objects.all().delete()
                self.assertMalformed("consent", consent=value)

    def test_a_non_dict_consent_entry_is_malformed(self):
        for value in (True, "granted", 1, ["granted"], None):
            with self.subTest(value=value):
                CommandLog.objects.all().delete()
                self.assertMalformed(
                    "consent", consent={**GRANTED_ALL, "marketing_sms": value}
                )


class LeadCreateTypeTests(LeadBase):
    def contact(self, **over):
        base = {"name": "Priya R.", "email": "priya@example.test", "phone_e164": ""}
        base.update(over)
        return base

    def assertMalformed(self, field, **over):
        with self.assertRaises(SchemaError) as cm:
            self.create(**over)
        row = CommandLog.objects.get(command="lead.create")
        self.assertEqual(row.outcome, "rejected")
        self.assertIn(field, " ".join(cm.exception.errors))
        self.assertIn(field, str(row.problem))
        self.assertNotIn(LEAK, row_text(row))
        self.assertFalse(Lead.all_tenants.exists())

    def test_a_non_text_email_is_malformed(self):
        for value in NOT_TEXT:
            with self.subTest(value=value):
                CommandLog.objects.all().delete()
                self.assertMalformed("email", contact=self.contact(email=value))

    def test_a_non_text_name_is_malformed(self):
        for value in NOT_TEXT:
            with self.subTest(value=value):
                CommandLog.objects.all().delete()
                self.assertMalformed("name", contact=self.contact(name=value))

    def test_a_non_text_phone_is_malformed(self):
        for value in NOT_TEXT:
            with self.subTest(value=value):
                CommandLog.objects.all().delete()
                self.assertMalformed("phone", contact=self.contact(phone_e164=value))

    def test_a_non_dict_contact_is_malformed(self):
        for value in ("priya", 5, ["x"], None):
            with self.subTest(value=value):
                CommandLog.objects.all().delete()
                self.assertMalformed("contact", contact=value)

    def test_a_non_dict_consent_is_malformed(self):
        for value in ("contact_by_business", 5, ["contact_by_business"], None):
            with self.subTest(value=value):
                CommandLog.objects.all().delete()
                self.assertMalformed("consent", consent=value)


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class ClaimViewTypeTests(TransactionTestCase):
    """The forms only ever hand the service strings, so a bad type is injected
    after form validation. The view must answer, not 500."""

    def setUp(self):
        self.enterContext(email_send_stub())
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme Directory", primary_domain=HOST
        )
        lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="Business",
            label_plural="Businesses", path_segment="businesses",
        )
        self.listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug="hoffman-plumbing",
            name="Hoffman Plumbing", visibility=Listing.Visibility.PUBLISHED,
        )

    def submit(self):
        return Client().post(
            f"/claim/{self.listing.public_id}/",
            {
                "name": "Dana Hoffman", "email": "dana@hoffmanplumbing.example",
                "phone_e164": "", "role_claimed": "owner", "method": "manual",
                "marketing_email": "on",
            },
            HTTP_HOST=HOST,
        )

    def test_a_non_dict_consent_is_a_form_error_not_a_500(self):
        with mock.patch.object(claim_views.ClaimForm, "consent_payload", return_value=7):
            r = self.submit()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "consent must be an object")
        row = CommandLog.objects.get(command="claim.submit")
        self.assertEqual(row.outcome, "rejected")

    def test_a_non_text_email_is_a_form_error_not_a_500(self):
        original = claim_views.ClaimForm.is_valid

        def is_valid(form):
            ok = original(form)
            form.cleaned_data["email"] = 123
            return ok

        with mock.patch.object(claim_views.ClaimForm, "is_valid", is_valid):
            r = self.submit()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "email must be text")
        row = CommandLog.objects.get(command="claim.submit")
        self.assertEqual(row.outcome, "rejected")


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class LeadViewTypeTests(LeadBase):
    def submit(self):
        self.n += 1
        return Client().post(
            f"/lead/{self.listing.public_id}/",
            {
                "name": "Priya R.", "email": f"priya{self.n}@example.test",
                "phone_e164": "", "message": f"{GOOD_MESSAGE} ({self.n})",
                "contact_by_business": "on", "company_url": "",
                "form_token": lead_views.issue_token(
                    timezone.now() - timedelta(seconds=30)
                ),
            },
            HTTP_HOST=HOST,
        )

    def test_a_non_dict_consent_is_a_form_error_not_a_500(self):
        with mock.patch.object(lead_views.LeadForm, "consent_payload", return_value=7):
            r = self.submit()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "consent must be an object")
        row = CommandLog.objects.get(command="lead.create")
        self.assertEqual(row.outcome, "rejected")

    def test_a_non_text_email_is_a_form_error_not_a_500(self):
        original = lead_views.LeadForm.is_valid

        def is_valid(form):
            ok = original(form)
            form.cleaned_data["email"] = 123
            return ok

        with mock.patch.object(lead_views.LeadForm, "is_valid", is_valid):
            r = self.submit()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "email must be text")
        row = CommandLog.objects.get(command="lead.create")
        self.assertEqual(row.outcome, "rejected")
