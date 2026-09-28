"""The public claim form: rendering, the masked contact hint, and a full
submission through the URLconf (spec §9, §9.4).

Submission goes through directory.services.submit_claim, which refuses to
run inside an open transaction -- so the POST tests use TransactionTestCase
(a plain TestCase wraps every test in one); the GET-only render tests don't
touch that guard and use TestCase like every other public-site test.
"""

from __future__ import annotations

from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from directory.models import Claim, Consent, Listing, ListingType
from tenants.models import InstallSetup, Tenant

HOST = "acme.test"


class _Base:
    def setUp(self):
        super().setUp()
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme Directory", primary_domain=HOST
        )
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="Business",
            label_plural="Businesses", path_segment="businesses",
        )
        self.listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="hoffman-plumbing",
            name="Hoffman Plumbing", visibility=Listing.Visibility.PUBLISHED,
            phone_e164="+17735550142", email="dana@hoffmanplumbing.example",
        )
        self.client = Client()

    def get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=HOST, **kw)

    def post(self, path, data, **kw):
        return self.client.post(path, data, HTTP_HOST=HOST, **kw)


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class ClaimFormRenderTests(_Base, TestCase):
    def test_renders_masked_contact_hint_not_the_real_values(self):
        r = self.get(f"/claim/{self.listing.public_id}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "+1••••42")
        self.assertContains(r, "d••••@hoffmanplumbing.example")
        self.assertNotContains(r, "+17735550142")
        self.assertNotContains(r, "dana@hoffmanplumbing.example")

    def test_unpublished_listing_404s(self):
        self.listing.visibility = Listing.Visibility.DRAFT
        self.listing.save(update_fields=["visibility"])
        r = self.get(f"/claim/{self.listing.public_id}/")
        self.assertEqual(r.status_code, 404)

    def test_method_choices_are_limited_to_tenant_enabled_methods(self):
        r = self.get(f"/claim/{self.listing.public_id}/")
        self.assertContains(r, 'value="manual"')
        self.assertNotContains(r, 'value="domain_email"')


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class ClaimFormSubmitTests(_Base, TransactionTestCase):
    def test_valid_submission_creates_a_claim_and_redirects(self):
        r = self.post(
            f"/claim/{self.listing.public_id}/",
            {
                "name": "Dana Hoffman",
                "email": "dana@hoffmanplumbing.example",
                "phone_e164": "+17735550142",
                "role_claimed": "owner",
                "method": "manual",
                "marketing_email": "on",
            },
        )
        self.assertEqual(r.status_code, 302)
        claim = Claim.all_tenants.get(tenant=self.tenant)
        self.assertIn(claim.public_id, r.url)
        self.assertEqual(claim.status, Claim.Status.PENDING_VERIFICATION)

    def test_a_spoofed_consent_text_version_field_is_ignored(self):
        # ClaimForm no longer declares this field (§9.0 -- the server decides
        # which wording a claimant "saw", never the client), so a value
        # injected straight into the POST body has no path to submit_claim at
        # all. The recorded Consent rows still carry the server's own version.
        r = self.post(
            f"/claim/{self.listing.public_id}/",
            {
                "name": "Dana Hoffman",
                "email": "dana@hoffmanplumbing.example",
                "phone_e164": "",
                "role_claimed": "owner",
                "method": "manual",
                "consent_text_version": "v999-not-real",
                "marketing_email": "on",
            },
        )
        self.assertEqual(r.status_code, 302)
        claim = Claim.all_tenants.get(tenant=self.tenant)
        recorded = Consent.all_tenants.filter(claim=claim).values_list(
            "text_version", flat=True
        )
        self.assertTrue(recorded)
        self.assertTrue(all(v == "consent-v1" for v in recorded))
