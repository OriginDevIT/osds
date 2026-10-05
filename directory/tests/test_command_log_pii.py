"""No claimant or lead PII in the command log (#219, decisions.md §4.4).

The command log is permanent and its rows are written outside the command
transaction, so a submitted email, name, phone number or message that lands in
one outlives the claim or lead it came from. Each test drives one
``log_received`` call site and asserts that no submitted value appears
anywhere in the row: actor, payload, problem or any other column.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.test import TransactionTestCase

from audit.models import CommandLog
from audit.ratelimit import RateLimited, Rule
from audit.tests.window_clock import pinned_windows
from directory import claim_limits, lead_limits, services
from directory.field_schema import SchemaError
from directory.models import Claim, Listing, ListingType
from directory.tests.lead_base import LeadBase
from osds.tenancy import tenant_context
from osds.tests.mail_stub import email_send_stub
from tenants.models import Tenant

GRANTED_ALL = {
    "marketing_email": {"granted": True},
    "marketing_sms": {"granted": True},
    "automated_calls": {"granted": False},
}

EMAIL = "dana.hoffman@hoffmanplumbing.example"
NAME = "Dana Hoffman"
PHONE = "+17735550142"
BAD_PHONE = "773-555-01"  # fails E.164 validation
MESSAGE = "Burst pipe under the sink, call Dana on her mobile."


def row_text(row: CommandLog) -> str:
    """Every column of the row, flattened and lowercased."""
    values = CommandLog.objects.filter(pk=row.pk).values().get()
    return json.dumps(values, default=str).lower()


class PiiAssertions:
    def assertNoPii(self, row, *needles):
        text = row_text(row)
        for needle in needles:
            self.assertNotIn(needle.lower(), text)


def one_per_minute(name):
    return (Rule(name, 1, timedelta(minutes=1)),)


class ClaimSubmitLogTests(PiiAssertions, TransactionTestCase):
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

    def submit(self, *, phone=PHONE, ip="203.0.113.44"):
        with tenant_context(self.tenant):
            return services.submit_claim(
                self.tenant,
                listing=self.listing,
                method="manual",
                claimant={
                    "name": NAME, "email": EMAIL, "phone_e164": phone,
                    "role_claimed": "owner",
                },
                consent=GRANTED_ALL,
                ip=ip,
            )

    def test_the_received_row_names_the_claim_and_carries_no_pii(self):
        claim = self.submit()
        row = CommandLog.objects.get(command="claim.submit")
        self.assertEqual(row.outcome, "applied")
        self.assertEqual(
            row.actor, {"type": "visitor", "id": f"claimant:{claim.public_id}"}
        )
        self.assertNoPii(row, EMAIL, NAME, PHONE, "hoffmanplumbing.example")

    def test_the_row_of_a_refused_submit_names_a_claim_that_was_never_made(self):
        # The id is minted before the transaction opens, so a rejected attempt
        # still has a non-PII reference; no claim row exists for it.
        with self.assertRaises(services.ConsentRequired):
            with tenant_context(self.tenant):
                services.submit_claim(
                    self.tenant,
                    listing=self.listing,
                    method="manual",
                    claimant={"name": NAME, "email": EMAIL, "phone_e164": PHONE},
                    consent={"marketing_email": {"granted": True}},
                    ip="203.0.113.44",
                )
        row = CommandLog.objects.get(command="claim.submit")
        self.assertEqual(row.outcome, "rejected")
        self.assertTrue(row.actor["id"].startswith("claimant:claim_"))
        self.assertFalse(Claim.all_tenants.exists())
        self.assertNoPii(row, EMAIL, NAME, PHONE)

    def test_the_blocked_row_carries_no_pii(self):
        with mock.patch.object(
            claim_limits, "SUBMIT_EMAIL", one_per_minute("claim.submit.email.24h")
        ):
            self.submit()
            with self.assertRaises(RateLimited):
                self.submit(ip="203.0.113.99")
        row = CommandLog.objects.get(command="claim.submit", outcome="blocked")
        self.assertTrue(row.actor["id"].startswith("claimant:claim_"))
        self.assertNoPii(row, EMAIL, NAME, PHONE)

    def test_the_malformed_row_does_not_echo_the_phone_number(self):
        with self.assertRaises(SchemaError):
            self.submit(phone=BAD_PHONE)
        row = CommandLog.objects.get(command="claim.submit")
        self.assertEqual(row.outcome, "rejected")
        self.assertNoPii(row, BAD_PHONE, EMAIL, NAME)


    def test_an_unknown_method_is_not_echoed_into_the_row(self):
        hostile = "mail dana.hoffman@hoffmanplumbing.example"
        with self.assertRaises(SchemaError):
            with tenant_context(self.tenant):
                services.submit_claim(
                    self.tenant,
                    listing=self.listing,
                    method=hostile,
                    claimant={"name": NAME, "email": EMAIL, "phone_e164": PHONE},
                    consent=GRANTED_ALL,
                    ip="203.0.113.44",
                )
        row = CommandLog.objects.get(command="claim.submit")
        self.assertEqual(row.outcome, "rejected")
        self.assertIn("method is not one of", row.problem["errors"][0])
        self.assertNoPii(row, hostile, EMAIL, "mail dana")


class LeadCreateLogTests(PiiAssertions, LeadBase):
    def contact(self, phone=PHONE):
        return {"name": NAME, "email": EMAIL, "phone_e164": phone}

    def test_the_received_row_carries_no_contact_or_message(self):
        self.create(contact=self.contact(), message=MESSAGE)
        row = CommandLog.objects.get(command="lead.create")
        self.assertEqual(row.outcome, "applied")
        self.assertEqual(row.payload["listing_id"], self.listing.public_id)
        self.assertNoPii(row, EMAIL, NAME, PHONE, MESSAGE, "mobile")

    def test_the_blocked_row_carries_no_contact_or_message(self):
        with mock.patch.object(
            lead_limits, "CREATE_EMAIL", one_per_minute("lead.create.email.24h")
        ):
            self.create(contact=self.contact(), message=MESSAGE)
            with self.assertRaises(RateLimited):
                self.create(
                    contact=self.contact(), message=MESSAGE + " Again.",
                    ip="198.51.100.99",
                )
        row = CommandLog.objects.get(command="lead.create", outcome="blocked")
        self.assertNoPii(row, EMAIL, NAME, PHONE, MESSAGE, "mobile")

    def test_the_malformed_row_does_not_echo_the_phone_number(self):
        with self.assertRaises(SchemaError):
            self.create(contact=self.contact(phone=BAD_PHONE), message=MESSAGE)
        row = CommandLog.objects.get(command="lead.create")
        self.assertEqual(row.outcome, "rejected")
        self.assertNoPii(row, BAD_PHONE, EMAIL, NAME, MESSAGE)

    def test_an_unknown_kind_is_not_echoed_into_the_row(self):
        hostile = "mail dana.hoffman@hoffmanplumbing.example"
        with self.assertRaises(SchemaError):
            self.create(contact=self.contact(), message=MESSAGE, kind=hostile)
        row = CommandLog.objects.get(command="lead.create")
        self.assertEqual(row.outcome, "rejected")
        self.assertIn("kind is not one of", row.problem["payload"])
        self.assertNoPii(row, hostile, EMAIL, "mail dana")
