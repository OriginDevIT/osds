"""directory.services.submit_claim -- the claim.submit command (spec §4.3,
§9, §9.0, §9.4): user matching, consent, the dispute branch, and the command
log.

submit_claim refuses to run inside an open transaction, so these tests use
TransactionTestCase (a plain TestCase wraps every test in one).
"""

from __future__ import annotations

from django.test import TransactionTestCase

from audit.models import CommandLog, OutboxEvent
from directory import services
from directory.field_schema import SchemaError
from directory.models import (
    Claim,
    Consent,
    ConsentText,
    DirectoryUser,
    Listing,
    ListingType,
)
from osds.tenancy import tenant_context
from tenants.models import Tenant

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


class _Base(TransactionTestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )
        self.listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="hoffman-plumbing",
            name="Hoffman Plumbing",
        )

    def submit(self, **overrides):
        kwargs = dict(
            listing=self.listing,
            method="manual",
            claimant=dict(DANA),
            consent=GRANTED_ALL,
            ip="203.0.113.44",
        )
        kwargs.update(overrides)
        with tenant_context(self.tenant):
            return services.submit_claim(self.tenant, **kwargs)

    def events(self, etype):
        return OutboxEvent.all_tenants.filter(type=etype, tenant=self.tenant)

    def event_types(self):
        return list(
            OutboxEvent.all_tenants.filter(tenant=self.tenant)
            .order_by("id")
            .values_list("type", flat=True)
        )


class DefaultConsentTextTests(_Base):
    def test_lazily_seeds_v1_with_a_placeholder_body(self):
        with tenant_context(self.tenant):
            ct = services.get_default_consent_text(self.tenant)
        self.assertEqual(ct.key, "consent")
        self.assertEqual(ct.version, "v1")
        self.assertIn("PLACEHOLDER", ct.body)

    def test_is_idempotent_get_or_create(self):
        with tenant_context(self.tenant):
            first = services.get_default_consent_text(self.tenant)
            second = services.get_default_consent_text(self.tenant)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            ConsentText.all_tenants.filter(tenant=self.tenant).count(), 1
        )


class SubmitClaimHappyPathTests(_Base):
    def test_mints_user_and_creates_a_pending_claim(self):
        claim = self.submit()
        self.assertEqual(claim.status, Claim.Status.PENDING_VERIFICATION)
        self.assertEqual(claim.method, "manual")
        self.assertEqual(claim.role_claimed, "owner")
        user = DirectoryUser.all_tenants.get(
            tenant=self.tenant, email="dana@hoffmanplumbing.example"
        )
        self.assertEqual(claim.claimant_id, user.id)

    def test_emits_user_created_then_claim_submitted_same_transaction(self):
        claim = self.submit()
        self.assertEqual(self.event_types(), ["user.created", "claim.submitted"])
        submitted = self.events("claim.submitted").get()
        self.assertEqual(submitted.subject, claim.public_id)
        self.assertEqual(
            submitted.data["claimant"]["email"], "dana@hoffmanplumbing.example"
        )
        self.assertEqual(submitted.data["claim"]["method"], "manual")

    def test_matching_an_existing_user_emits_no_user_created(self):
        DirectoryUser.all_tenants.create(
            tenant=self.tenant,
            email="dana@hoffmanplumbing.example",
            name="Old Name",
        )
        self.submit()
        self.assertFalse(self.events("user.created").exists())
        self.assertEqual(self.event_types(), ["claim.submitted"])
        self.assertEqual(
            DirectoryUser.all_tenants.filter(
                tenant=self.tenant, email="dana@hoffmanplumbing.example"
            ).count(),
            1,
        )

    def test_matching_an_existing_user_does_not_overwrite_its_fields(self):
        DirectoryUser.all_tenants.create(
            tenant=self.tenant,
            email="dana@hoffmanplumbing.example",
            name="Old Name",
            phone_e164="+15550000000",
        )
        self.submit()
        user = DirectoryUser.all_tenants.get(
            tenant=self.tenant, email="dana@hoffmanplumbing.example"
        )
        self.assertEqual(user.name, "Old Name")
        self.assertEqual(user.phone_e164, "+15550000000")

    def test_email_is_matched_lowercased(self):
        self.submit(claimant={**DANA, "email": "Dana@HoffmanPlumbing.EXAMPLE"})
        self.assertTrue(
            DirectoryUser.all_tenants.filter(
                tenant=self.tenant, email="dana@hoffmanplumbing.example"
            ).exists()
        )

    def test_writes_three_consent_rows_declined_channel_null_at_and_ip(self):
        claim = self.submit()
        rows = {c.channel: c for c in Consent.all_tenants.filter(claim=claim)}
        self.assertEqual(
            set(rows), {"marketing_email", "marketing_sms", "automated_calls"}
        )
        self.assertTrue(rows["marketing_email"].granted)
        self.assertIsNotNone(rows["marketing_email"].granted_at)
        self.assertEqual(rows["marketing_email"].ip, "203.0.113.44")
        self.assertEqual(rows["marketing_email"].text_version, "consent-v1")
        self.assertFalse(rows["automated_calls"].granted)
        self.assertIsNone(rows["automated_calls"].granted_at)
        self.assertIsNone(rows["automated_calls"].ip)

    def test_writes_an_applied_command_log_row(self):
        self.submit()
        row = CommandLog.objects.get(command="claim.submit")
        self.assertEqual(row.outcome, "applied")
        self.assertTrue(row.result_event_id)
        self.assertEqual(row.payload["claimant"]["email"], "dana@hoffmanplumbing.example")


class SubmitClaimConsentTests(_Base):
    def test_missing_a_consent_channel_rejects_and_writes_nothing(self):
        with self.assertRaises(services.ConsentRequired):
            self.submit(
                consent={
                    "marketing_email": {"granted": True},
                    "marketing_sms": {"granted": True},
                }
            )
        self.assertFalse(Claim.all_tenants.filter(tenant=self.tenant).exists())
        self.assertFalse(self.events("claim.submitted").exists())
        row = CommandLog.objects.get(command="claim.submit")
        self.assertEqual(row.outcome, "rejected")
        self.assertEqual(row.problem["missing_consent"], "automated_calls")

    def test_text_version_is_not_an_accepted_parameter(self):
        # §9.0: the recorded version has to be what the server actually
        # showed, never something a caller (a visitor's own form field)
        # supplies. Removed from the signature entirely, not just ignored.
        with self.assertRaises(TypeError):
            self.submit(consent_text_version="v999")

    def test_missing_claimant_email_rejects_before_a_real_payload_is_logged(self):
        with self.assertRaises(SchemaError):
            self.submit(claimant={**DANA, "email": ""})
        row = CommandLog.objects.get(command="claim.submit")
        self.assertEqual(row.outcome, "rejected")
        self.assertIsNone(row.payload)
        self.assertFalse(Claim.all_tenants.filter(tenant=self.tenant).exists())


class SubmitClaimMethodTests(_Base):
    def test_method_not_enabled_for_tenant_is_rejected(self):
        # No claim_verification configured -> only "manual" is available.
        with self.assertRaises(SchemaError):
            self.submit(method="domain_email")
        self.assertFalse(Claim.all_tenants.filter(tenant=self.tenant).exists())

    def test_unknown_method_is_rejected(self):
        with self.assertRaises(SchemaError):
            self.submit(method="carrier_pigeon")

    def test_enabled_methods_from_tenant_settings_are_accepted(self):
        self.tenant.settings["claim_verification"] = {
            "enabled_methods": ["manual", "domain_email"]
        }
        self.tenant.save(update_fields=["settings"])
        claim = self.submit(method="domain_email")
        self.assertEqual(claim.method, "domain_email")


class SubmitClaimDisputeTests(_Base):
    def test_two_pending_claims_on_an_unclaimed_listing_both_proceed(self):
        first = self.submit()
        second = self.submit(
            claimant={
                "name": "Someone Else",
                "email": "else@example.test",
                "phone_e164": "",
                "role_claimed": "owner",
            }
        )
        self.assertEqual(first.status, Claim.Status.PENDING_VERIFICATION)
        self.assertEqual(second.status, Claim.Status.PENDING_VERIFICATION)
        self.assertFalse(self.events("claim.disputed").exists())
        self.assertFalse(self.events("moderation.queued").exists())

    def test_second_claim_on_an_already_claimed_listing_disputes(self):
        self.listing.status = Listing.Status.CLAIMED
        self.listing.save(update_fields=["status"])

        claim = self.submit()

        claim.refresh_from_db()
        self.assertEqual(claim.status, Claim.Status.DISPUTED)
        self.assertEqual(
            self.event_types(),
            ["user.created", "claim.submitted", "claim.disputed", "moderation.queued"],
        )
        item = self.events("moderation.queued").get()
        self.assertEqual(item.data["item_type"], "claim_dispute")
        self.assertEqual(item.data["item_id"], claim.public_id)
        self.assertEqual(item.data["rules_triggered"], ["duplicate_claim"])
        # Verification alone never moves ownership away from a sitting owner
        # (spec §9.4) -- the listing itself is untouched by this PR.
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, Listing.Status.CLAIMED)

    def test_dispute_does_not_write_listing_status_or_owner(self):
        self.listing.status = Listing.Status.CLAIMED
        self.listing.save(update_fields=["status"])
        before_owner_id = self.listing.owner_id
        self.submit()
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, Listing.Status.CLAIMED)
        self.assertEqual(self.listing.owner_id, before_owner_id)
