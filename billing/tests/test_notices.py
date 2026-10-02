"""The owner's billing mail (decisions.md §4.11): one message per consequence,
with a producer deadline, in the transaction that caused it."""

from __future__ import annotations

from datetime import timedelta

from audit.models import OutboundMessage
from billing import machine
from billing.machine import DUNNING, GRACE, Trigger
from billing.models import Entitlement
from billing.tests.base import DAY, T0, BillingTestCase, Role
from directory.models import DirectoryUser, Listing
from tenants.models import Tenant

LINK = "https://acme.test/owner/listings/"


class _Base(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.manager = self.operator(Role.MANAGER)
        self.owner = DirectoryUser.all_tenants.create(
            tenant=self.tenant, email="owner@acme-co.example", name="Zed Unique-Name"
        )
        Listing.all_tenants.filter(pk=self.listing.pk).update(owner=self.owner, status="claimed")
        self.listing = Listing.all_tenants.get(pk=self.listing.pk)

    def mail(self, kind=None):
        qs = OutboundMessage.all_tenants.filter(tenant=self.tenant).order_by("id")
        return qs.filter(kind=kind) if kind else qs

    def start(self):
        self.apply(Trigger.START, tier=self.featured, period_end=T0 + 30 * DAY)


class NoticeTests(_Base):
    def test_a_failed_payment_tells_the_owner_until_the_dunning_window_ends(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, failure_code="x", now=T0 + 30 * DAY)
        m = self.mail("billing.payment_failed").get()
        self.assertEqual(m.to_address, "owner@acme-co.example")
        self.assertEqual(m.subject, "Payment failed for Acme Co")
        self.assertEqual(m.expires_at, T0 + 30 * DAY + DUNNING)
        self.assertIn("keeps its current perks", m.body_text)
        self.assertIn(f"{LINK}{self.listing.public_id}/", m.body_text)
        self.assertIn(f"{(T0 + 30 * DAY + DUNNING):%Y-%m-%d}", m.body_text)

    def test_downgrade_until_grace_ends(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, failure_code="x", now=T0)
        self.apply(Trigger.DUNNING_ELAPSED, now=T0 + DUNNING)
        m = self.mail("billing.downgraded").get()
        self.assertEqual(m.expires_at, T0 + DUNNING + GRACE)
        self.assertIn("still published", m.body_text)

    def test_expiry_lasts_a_week(self):
        self.start()
        self.apply(Trigger.REFUND, now=T0 + DAY)
        m = self.mail("billing.expired").get()
        self.assertEqual(m.expires_at, T0 + DAY + timedelta(days=7))
        self.assertIn("still published", m.body_text)

    def test_cancel_confirms_until_the_period_end(self):
        self.start()
        self.apply(Trigger.CANCEL, now=T0 + DAY, canceled_by="owner")
        m = self.mail("billing.canceled").get()
        self.assertEqual(m.expires_at, T0 + 30 * DAY)
        self.assertIn(f"{(T0 + 30 * DAY):%Y-%m-%d}", m.body_text)
        self.assertFalse(self.mail("billing.expired").exists())

    def test_cancel_from_grace_sends_one_message_not_two(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, failure_code="x", now=T0)
        self.apply(Trigger.DUNNING_ELAPSED, now=T0 + DUNNING)
        self.apply(Trigger.CANCEL, now=T0 + DUNNING + DAY, canceled_by="owner")
        self.assertEqual(self.mail("billing.canceled").count(), 1)
        self.assertEqual(self.mail("billing.expired").count(), 0)
        self.assertIn("has ended", self.mail("billing.canceled").get().body_text)

    def test_an_ended_subscription_is_an_expiry_notice(self):
        self.start()
        self.apply(Trigger.SUBSCRIPTION_ENDED)
        self.assertEqual(self.mail("billing.expired").count(), 1)

    def test_a_comp_ending_and_an_override_say_nothing(self):
        self.apply(Trigger.GRANT, tier=self.featured, operator=self.manager, reason="r")
        self.apply(Trigger.REVOKE, operator=self.manager, reason="r")
        other = self.make_listing("o2", "O2")
        Listing.all_tenants.filter(pk=other.pk).update(owner=self.owner)
        self.apply(Trigger.GRANT, listing=other, tier=self.featured, operator=self.manager, reason="r",
                   expires_at=T0 + DAY)
        self.apply(Trigger.COMP_EXPIRED, listing=other, now=T0 + DAY)
        self.apply(Trigger.OVERRIDE, to_status="active", tier=self.featured, operator=self.manager, reason="fix")
        self.assertFalse(self.mail().exists())

    def test_nothing_is_sent_for_a_started_or_recovered_subscription(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, failure_code="x", now=T0 + 30 * DAY)
        before = self.mail().count()
        self.apply(Trigger.PAYMENT_SUCCEEDED, now=T0 + 31 * DAY, period_end=T0 + 60 * DAY)
        self.assertEqual(self.mail().count(), before)

    def test_a_listing_with_no_owner_or_no_address_gets_no_mail_and_no_error(self):
        Listing.all_tenants.filter(pk=self.listing.pk).update(owner=None)
        self.start()
        self.apply(Trigger.REFUND)
        DirectoryUser.all_tenants.filter(pk=self.owner.pk).update(email="")
        Listing.all_tenants.filter(pk=self.listing.pk).update(owner=self.owner)
        other = self.make_listing("o3", "O3")
        Listing.all_tenants.filter(pk=other.pk).update(owner=self.owner)
        self.apply(Trigger.START, listing=other, tier=self.featured, period_end=T0 + DAY)
        self.apply(Trigger.REFUND, listing=other)
        self.assertFalse(self.mail().exists())

    def test_without_an_absolute_base_the_mail_has_no_link(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(domain_verified_at=None)
        self.tenant.refresh_from_db()
        self.start()
        self.apply(Trigger.REFUND)
        body = self.mail("billing.expired").get().body_text
        self.assertNotIn("http", body)
        self.assertIn("Sign in to your owner page", body)

    def test_the_mail_carries_nothing_a_claimant_wrote(self):
        self.start()
        self.apply(Trigger.REFUND)
        m = self.mail("billing.expired").get()
        self.assertNotIn("Zed", m.subject + m.body_text)
        self.assertNotIn("Unique-Name", m.subject + m.body_text)

    def test_the_message_is_queued_in_the_same_transaction_as_the_change(self):
        from django.db import transaction

        from billing import entitlements

        self.start()

        class Boom(Exception):
            pass

        try:
            with transaction.atomic():
                self.apply(Trigger.REFUND)
                raise Boom
        except Boom:
            pass
        self.assertEqual(self.ent().status, Entitlement.Status.ACTIVE)
        self.assertFalse(self.mail("billing.expired").exists())
        self.assertIsNotNone(entitlements)

    def test_the_tick_sends_them_too(self):
        from billing import jobs

        self.start()
        self.apply(Trigger.PAYMENT_FAILED, failure_code="x", now=T0)
        jobs.entitlement_transitions(now=T0 + DUNNING)
        self.assertEqual(self.mail("billing.downgraded").count(), 1)
        jobs.entitlement_transitions(now=T0 + DUNNING + GRACE)
        self.assertEqual(self.mail("billing.expired").count(), 1)
        self.assertIsNotNone(machine)
