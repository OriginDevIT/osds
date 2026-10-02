"""The entitlement service beyond the table: payloads, timers, causes, the tier
writer, visibility on expiry, and what the public page shows (spec §6;
decisions.md §4.11)."""

from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import Client, override_settings

from billing import entitlements, machine
from billing.machine import GRACE, DUNNING, InvalidTransition, NotDue, Trigger
from billing.models import Entitlement, Tier
from billing.tests.base import DAY, T0, BillingTestCase, Role
from directory.models import Listing

S = Entitlement.Status


class _Base(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.operator(Role.MANAGER)

    def start(self, tier=None, **kw):
        return self.apply(Trigger.START, tier=tier or self.featured,
                          period_end=kw.pop("period_end", T0 + 30 * DAY), **kw)

    def payload(self, type_, **kw):
        return self.events(type_, **kw).order_by("id").last().data


class PayloadTests(_Base):
    def test_started(self):
        self.start(payment_ref={"adapter": "x", "external_id": "sub_1"})
        data = self.payload("entitlement.started")
        self.assertEqual(data["tier"], "featured")
        self.assertEqual(data["billing_mode"], "recurring")
        self.assertEqual(data["period_end"], (T0 + 30 * DAY).isoformat())
        self.assertIsNone(data["trial_ends_at"])
        self.assertEqual(self.ent().payment_ref, {"adapter": "x", "external_id": "sub_1"})
        started = self.events("entitlement.started").get()
        self.assertEqual(started.subject, self.ent().public_id)

    def test_started_with_a_trial(self):
        self.apply(Trigger.START_TRIAL, tier=self.featured, trial_ends_at=T0 + 7 * DAY)
        ent = self.ent()
        self.assertEqual((ent.status, ent.current_period_end), (S.TRIALING, T0 + 7 * DAY))
        self.assertEqual(self.payload("entitlement.started")["trial_ends_at"], (T0 + 7 * DAY).isoformat())

    def test_trial_converted(self):
        self.apply(Trigger.START_TRIAL, tier=self.featured, trial_ends_at=T0 + 7 * DAY)
        self.apply(Trigger.PAYMENT_SUCCEEDED, now=T0 + 7 * DAY, period_end=T0 + 37 * DAY)
        self.assertEqual(self.ent().status, S.ACTIVE)
        self.assertEqual(self.payload("entitlement.trial_converted")["period_end"], (T0 + 37 * DAY).isoformat())

    def test_dunning_started_carries_attempt_deadline_and_code(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, now=T0 + 30 * DAY, failure_code="card_declined", attempt=2)
        data = self.payload("entitlement.dunning_started")
        self.assertEqual(data["attempt"], 2)
        self.assertEqual(data["failure_code"], "card_declined")
        self.assertEqual(data["dunning_ends_at"], (T0 + 30 * DAY + DUNNING).isoformat())
        ent = self.ent()
        self.assertEqual((ent.failed_attempts, ent.last_failure_code), (2, "card_declined"))

    def test_a_further_failure_while_past_due_counts_but_emits_nothing(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, now=T0 + DAY, failure_code="a")
        since = self.last_event_id()
        self.apply(Trigger.PAYMENT_FAILED, now=T0 + 3 * DAY, failure_code="b")
        ent = self.ent()
        self.assertEqual((ent.status, ent.failed_attempts, ent.last_failure_code), (S.PAST_DUE, 2, "b"))
        self.assertEqual(self.events(since=since).count(), 0)
        self.assertEqual(ent.dunning_ends_at, T0 + DAY + DUNNING)  # the clock does not restart

    def test_recovered_reports_the_days_in_dunning(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, now=T0 + DAY, failure_code="x")
        self.apply(Trigger.PAYMENT_SUCCEEDED, now=T0 + 6 * DAY, period_end=T0 + 60 * DAY)
        self.assertEqual(self.payload("entitlement.recovered")["days_in_dunning"], 5)
        ent = self.ent()
        self.assertEqual((ent.status, ent.failed_attempts, ent.dunning_started_at), (S.ACTIVE, 0, None))

    def test_downgraded_names_both_tiers_and_the_grace_deadline(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, now=T0, failure_code="x")
        self.apply(Trigger.DUNNING_ELAPSED, now=T0 + DUNNING)
        data = self.payload("entitlement.downgraded")
        self.assertEqual((data["from_tier"], data["to_tier"]), ("featured", "free"))
        self.assertEqual(data["grace_ends_at"], (T0 + DUNNING + GRACE).isoformat())

    def test_restored_reports_the_days_in_grace(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, now=T0, failure_code="x")
        self.apply(Trigger.DUNNING_ELAPSED, now=T0 + DUNNING)
        self.apply(Trigger.PAYMENT_SUCCEEDED, now=T0 + DUNNING + 9 * DAY, period_end=T0 + 90 * DAY)
        self.assertEqual(self.payload("entitlement.restored")["days_in_grace"], 9)
        self.assertEqual(self.fresh().current_tier, self.featured)

    def test_canceled_at_period_end_and_the_expiry_that_follows(self):
        self.start()
        self.apply(Trigger.CANCEL, now=T0 + DAY, canceled_by="owner", reason="closing")
        data = self.payload("entitlement.canceled")
        self.assertEqual((data["at_period_end"], data["canceled_by"], data["reason"]), (True, "owner", "closing"))
        ent = self.ent()
        self.assertEqual((ent.status, ent.cancel_at_period_end, ent.current_period_end), (S.CANCELED, True, T0 + 30 * DAY))
        self.assertEqual(self.fresh().current_tier, self.featured)  # runs to the period end
        with self.assertRaises(NotDue):
            self.apply(Trigger.PERIOD_ELAPSED, now=T0 + 30 * DAY - timedelta(seconds=1))
        self.apply(Trigger.PERIOD_ELAPSED, now=T0 + 30 * DAY)
        self.assertEqual(self.payload("entitlement.expired")["cause"], "period_ended")
        self.assertEqual(self.fresh().current_tier, self.free)

    def test_expired_causes(self):
        cases = [
            (Trigger.REFUND, {}, "refund"),
            (Trigger.SUBSCRIPTION_ENDED, {}, "canceled"),
        ]
        for i, (trigger, params, cause) in enumerate(cases):
            with self.subTest(trigger=trigger):
                listing = self.make_listing(f"c{i}", f"C{i}")
                self.apply(Trigger.START, listing=listing, tier=self.featured, period_end=T0 + DAY)
                self.apply(trigger, listing=listing, **params)
                self.assertEqual(self.payload("entitlement.expired")["cause"], cause)
                self.assertEqual(self.payload("entitlement.expired")["from_tier"], "featured")

    def test_refund_emits_both_expired_and_tier_changed(self):
        self.start()
        since = self.last_event_id()
        self.apply(Trigger.REFUND)
        types = [e.type for e in self.events(since=since)]
        self.assertEqual(types, ["entitlement.expired", "listing.tier_changed"])
        self.assertEqual(self.payload("listing.tier_changed")["cause"], "refund")

    def test_overridden_records_who_why_and_the_move(self):
        self.start()
        self.apply(Trigger.OVERRIDE, to_status=S.EXPIRED, operator=self.admin, reason="chargeback")
        data = self.payload("entitlement.overridden")
        self.assertEqual(data["admin_id"], self.admin.public_id)
        self.assertEqual((data["reason"], data["from"], data["to"]), ("chargeback", "active", "expired"))

    def test_grant_records_the_comp_and_emits_overridden(self):
        expires = T0 + 90 * DAY
        self.apply(Trigger.GRANT, tier=self.featured, operator=self.admin, reason="partner", expires_at=expires)
        ent = self.ent()
        self.assertEqual((ent.status, ent.billing_mode, ent.comp_reason, ent.comp_expires_at),
                         (S.COMPED, "comp", "partner", expires))
        self.assertEqual(ent.comp_granted_by, self.admin)
        self.assertEqual(self.payload("entitlement.overridden")["to"], "comped")

    def test_the_revoke_ends_a_comp_with_all_three_events(self):
        self.apply(Trigger.GRANT, tier=self.featured, operator=self.admin, reason="r")
        since = self.last_event_id()
        self.apply(Trigger.REVOKE, operator=self.admin, reason="done")
        types = [e.type for e in self.events(since=since)]
        self.assertEqual(types, ["entitlement.overridden", "entitlement.expired", "listing.tier_changed"])
        self.assertEqual(self.payload("entitlement.expired")["cause"], "revoked")
        self.assertEqual(self.ent().comp_reason, "")


class TimerBoundaryTests(_Base):
    def test_past_due_to_grace_at_exactly_fourteen_days(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, now=T0, failure_code="x")
        with self.assertRaises(NotDue):
            self.apply(Trigger.DUNNING_ELAPSED, now=T0 + DUNNING - timedelta(seconds=1))
        self.assertEqual(self.ent().status, S.PAST_DUE)
        self.apply(Trigger.DUNNING_ELAPSED, now=T0 + DUNNING)
        self.assertEqual(self.ent().status, S.GRACE)

    def test_grace_to_expired_thirty_days_after_it_began(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, now=T0, failure_code="x")
        entered = T0 + DUNNING + 2 * DAY  # the tick was late
        self.apply(Trigger.DUNNING_ELAPSED, now=entered)
        with self.assertRaises(NotDue):
            self.apply(Trigger.GRACE_ELAPSED, now=entered + GRACE - timedelta(seconds=1))
        self.apply(Trigger.GRACE_ELAPSED, now=entered + GRACE)
        self.assertEqual(self.ent().status, S.EXPIRED)

    def test_a_trial_may_go_three_days_unreported(self):
        self.apply(Trigger.START_TRIAL, tier=self.featured, trial_ends_at=T0 + 7 * DAY)
        end = T0 + 7 * DAY
        with self.assertRaises(NotDue):
            self.apply(Trigger.TRIAL_OVERDUE, now=end + machine.TRIAL_OVERDUE - timedelta(seconds=1))
        self.apply(Trigger.TRIAL_OVERDUE, now=end + machine.TRIAL_OVERDUE)
        ent = self.ent()
        self.assertEqual((ent.status, ent.last_failure_code), (S.PAST_DUE, "trial_unreported"))
        self.assertEqual(self.payload("entitlement.dunning_started")["failure_code"], "trial_unreported")

    def test_a_comp_without_expiry_never_expires_and_one_with_expiry_does(self):
        self.apply(Trigger.GRANT, tier=self.featured, operator=self.admin, reason="r")
        with self.assertRaises(NotDue):
            self.apply(Trigger.COMP_EXPIRED, now=T0 + 3650 * DAY)
        self.apply(Trigger.GRANT, tier=self.featured, operator=self.admin, reason="r", expires_at=T0 + DAY)
        with self.assertRaises(NotDue):
            self.apply(Trigger.COMP_EXPIRED, now=T0 + DAY - timedelta(seconds=1))
        self.apply(Trigger.COMP_EXPIRED, now=T0 + DAY)
        self.assertEqual(self.ent().status, S.EXPIRED)

    def test_a_term_ends_only_in_term_mode(self):
        self.start(billing_mode="term", term_days=30)
        with self.assertRaises(NotDue):
            self.apply(Trigger.TERM_ENDED, now=T0 + 30 * DAY - timedelta(seconds=1))
        self.apply(Trigger.TERM_ENDED, now=T0 + 30 * DAY)
        self.assertEqual(self.payload("entitlement.expired")["cause"], "term_ended")
        other = self.make_listing("rec", "Rec")
        self.apply(Trigger.START, listing=other, tier=self.featured, period_end=T0 + DAY)
        with self.assertRaises(NotDue):
            self.apply(Trigger.TERM_ENDED, listing=other, now=T0 + 365 * DAY)


class CancelRuleTests(_Base):
    def test_cancel_from_past_due_skips_grace(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, now=T0 + 30 * DAY, failure_code="x")
        self.apply(Trigger.CANCEL, now=T0 + 31 * DAY, canceled_by="owner")
        self.assertEqual(self.ent().status, S.CANCELED)
        self.apply(Trigger.PERIOD_ELAPSED, now=T0 + 31 * DAY)  # the period had ended already
        self.assertEqual(self.ent().status, S.EXPIRED)
        self.assertFalse(self.events("entitlement.downgraded").exists())

    def test_cancel_from_grace_ends_it_at_once(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, now=T0, failure_code="x")
        self.apply(Trigger.DUNNING_ELAPSED, now=T0 + DUNNING)
        since = self.last_event_id()
        self.apply(Trigger.CANCEL, now=T0 + DUNNING + DAY, canceled_by="owner")
        self.assertEqual(self.ent().status, S.EXPIRED)
        types = [e.type for e in self.events(since=since)]
        self.assertEqual(types, ["entitlement.canceled", "entitlement.expired"])
        self.assertEqual(self.payload("entitlement.canceled")["at_period_end"], False)
        self.assertEqual(self.payload("entitlement.expired")["cause"], "canceled")

    def test_resubscribing_before_the_period_ends_restores_it(self):
        self.start()
        self.apply(Trigger.CANCEL, now=T0 + DAY, canceled_by="owner")
        self.apply(Trigger.PAYMENT_SUCCEEDED, now=T0 + 2 * DAY, period_end=T0 + 60 * DAY)
        ent = self.ent()
        self.assertEqual((ent.status, ent.cancel_at_period_end, ent.canceled_at), (S.ACTIVE, False, None))

    def test_a_comped_listing_that_buys_becomes_active_and_the_comp_clears(self):
        self.apply(Trigger.GRANT, tier=self.verified, operator=self.admin, reason="r")
        self.apply(Trigger.PAYMENT_SUCCEEDED, tier=self.featured, period_end=T0 + 30 * DAY)
        ent = self.ent()
        self.assertEqual((ent.status, ent.comp_reason, ent.comp_granted_by, ent.billing_mode),
                         (S.ACTIVE, "", None, "recurring"))
        self.assertEqual(self.fresh().current_tier, self.featured)


class TierWriterTests(_Base):
    def test_causes_are_the_defined_set(self):
        seen = set()
        self.apply(Trigger.START, tier=self.verified, period_end=T0 + 30 * DAY)
        seen.add(self.payload("listing.tier_changed")["cause"])           # purchase
        self.apply(Trigger.PAYMENT_SUCCEEDED, tier=self.featured, period_end=T0 + 60 * DAY)
        seen.add(self.payload("listing.tier_changed")["cause"])           # upgrade
        self.apply(Trigger.PAYMENT_SUCCEEDED, tier=self.verified, period_end=T0 + 90 * DAY)
        seen.add(self.payload("listing.tier_changed")["cause"])           # downgrade
        self.apply(Trigger.PAYMENT_FAILED, failure_code="x")
        self.apply(Trigger.DUNNING_ELAPSED, now=T0 + DUNNING)
        seen.add(self.payload("listing.tier_changed")["cause"])           # grace
        self.apply(Trigger.PAYMENT_SUCCEEDED, now=T0 + DUNNING + DAY, period_end=T0 + 120 * DAY)
        seen.add(self.payload("listing.tier_changed")["cause"])           # restore
        self.apply(Trigger.GRANT, tier=self.featured, operator=self.admin, reason="r")
        seen.add(self.payload("listing.tier_changed")["cause"])           # comp
        self.apply(Trigger.REVOKE, operator=self.admin, reason="r")
        seen.add(self.payload("listing.tier_changed")["cause"])           # revoke
        self.apply(Trigger.OVERRIDE, to_status=S.ACTIVE, tier=self.featured, operator=self.admin, reason="r")
        seen.add(self.payload("listing.tier_changed")["cause"])           # override
        self.apply(Trigger.REFUND)
        seen.add(self.payload("listing.tier_changed")["cause"])           # refund
        self.assertEqual(seen, {"purchase", "upgrade", "downgrade", "grace", "restore",
                                "comp", "revoke", "override", "refund"})

    def test_tier_changed_carries_from_to_and_the_effective_time(self):
        self.start()
        data = self.payload("listing.tier_changed")
        self.assertEqual((data["from_tier"], data["to_tier"], data["effective_at"]),
                         (None, "featured", T0.isoformat()))

    def test_no_event_when_the_effective_tier_does_not_move(self):
        self.start()
        since = self.last_event_id()
        self.apply(Trigger.PAYMENT_FAILED, failure_code="x")  # past_due keeps full perks
        self.assertFalse(self.events("listing.tier_changed", since=since).exists())
        self.assertEqual(self.fresh().current_tier, self.featured)


class VisibilityTests(_Base):
    with_rank0 = False

    def test_without_a_rank0_tier_expiry_hides_the_listing_and_a_repurchase_unhides_it(self):
        self.start()
        self.assertEqual(self.fresh().visibility, Listing.Visibility.PUBLISHED)
        self.apply(Trigger.REFUND)
        listing = self.fresh()
        self.assertEqual(listing.visibility, Listing.Visibility.HIDDEN)
        self.assertIsNone(listing.current_tier)
        self.assertTrue(self.ent().hidden_by_expiry)
        self.assertTrue(self.events("listing.unpublished").exists())
        self.assertEqual(self.payload("listing.tier_changed")["to_tier"], None)

        self.apply(Trigger.START, tier=self.featured, period_end=T0 + 90 * DAY, now=T0 + 40 * DAY)
        listing = self.fresh()
        self.assertEqual(listing.visibility, Listing.Visibility.PUBLISHED)
        self.assertFalse(self.ent().hidden_by_expiry)
        self.assertTrue(self.events("listing.published").exists())

    def test_a_listing_someone_else_unpublished_is_not_republished(self):
        self.start()
        self.apply(Trigger.REFUND)
        Listing.all_tenants.filter(pk=self.listing.pk).update(visibility=Listing.Visibility.DRAFT)
        self.apply(Trigger.START, tier=self.featured, period_end=T0 + 90 * DAY, now=T0 + 40 * DAY)
        self.assertEqual(self.fresh().visibility, Listing.Visibility.DRAFT)
        self.assertFalse(self.ent().hidden_by_expiry)

    def test_a_listing_that_was_not_published_is_not_marked(self):
        Listing.all_tenants.filter(pk=self.listing.pk).update(visibility=Listing.Visibility.DRAFT)
        self.start()
        self.apply(Trigger.REFUND)
        self.assertEqual(self.fresh().visibility, Listing.Visibility.DRAFT)
        self.assertFalse(self.ent().hidden_by_expiry)

    def test_with_a_rank0_tier_expiry_never_unpublishes(self):
        Tier.all_tenants.create(tenant=self.tenant, key="free", name="Free", rank=0)
        self.start()
        self.apply(Trigger.REFUND)
        listing = self.fresh()
        self.assertEqual(listing.visibility, Listing.Visibility.PUBLISHED)
        self.assertEqual(listing.current_tier.key, "free")
        self.assertFalse(self.ent().hidden_by_expiry)
        self.assertFalse(self.events("listing.unpublished").exists())

    def test_only_expiry_hides(self):
        self.start()
        self.apply(Trigger.PAYMENT_FAILED, failure_code="x")
        self.apply(Trigger.DUNNING_ELAPSED, now=T0 + DUNNING)  # grace: perks off, still published
        self.assertEqual(self.fresh().visibility, Listing.Visibility.PUBLISHED)
        self.assertIsNone(self.fresh().current_tier)


class NeverPurchasedTests(_Base):
    def test_a_listing_with_no_entitlement_is_untouched_by_anything_here(self):
        untouched = self.make_listing("never", "Never")
        other = self.make_listing("bought", "Bought")
        self.apply(Trigger.START, listing=other, tier=self.featured, period_end=T0 + DAY)
        self.apply(Trigger.REFUND, listing=other)
        listing = self.fresh(untouched)
        self.assertEqual((listing.current_tier, listing.visibility), (None, Listing.Visibility.PUBLISHED))
        self.assertFalse(Entitlement.all_tenants.filter(listing=untouched).exists())

    def test_clock_and_payment_triggers_need_an_entitlement(self):
        for trigger in (Trigger.PAYMENT_SUCCEEDED, Trigger.PAYMENT_FAILED, Trigger.CANCEL,
                        Trigger.REFUND, Trigger.DUNNING_ELAPSED):
            with self.subTest(trigger=trigger):
                with self.assertRaises(InvalidTransition):
                    self.apply(trigger, listing=self.make_listing(f"n-{trigger}", "N"),
                               period_end=T0 + DAY)


class IntegrityTests(_Base):
    def test_one_entitlement_per_listing(self):
        self.start()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Entitlement.all_tenants.create(
                tenant=self.tenant, listing=self.listing, tier=self.verified,
            )

    def test_missing_parameters_write_nothing_not_even_the_row(self):
        since = self.last_event_id()
        with self.assertRaises(entitlements.EntitlementError):
            self.apply(Trigger.START, tier=self.featured)  # no period_end
        with self.assertRaises(entitlements.EntitlementError):
            self.apply(Trigger.GRANT, tier=self.featured, operator=self.admin)  # no reason
        with self.assertRaises(entitlements.EntitlementError):
            self.apply(Trigger.START)  # no tier
        self.assertFalse(Entitlement.all_tenants.filter(listing=self.listing).exists())
        self.assertEqual(self.events(since=since).count(), 0)
        self.assertIsNone(self.fresh().current_tier)

    def test_events_carry_the_entitlement_and_the_listing(self):
        self.start()
        data = self.payload("entitlement.started")
        self.assertEqual(data["entitlement_id"], self.ent().public_id)
        self.assertEqual(data["listing_id"], self.listing.public_id)

    def test_every_event_this_service_emits_is_a_registered_type(self):
        from audit import events as registry

        self.start()
        self.apply(Trigger.PAYMENT_FAILED, failure_code="x")
        self.apply(Trigger.DUNNING_ELAPSED, now=T0 + DUNNING)
        self.apply(Trigger.REFUND)
        for e in self.events():
            self.assertTrue(registry.is_known_event_type(e.type), e.type)


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class PublicRenderingTests(_Base):
    """Spec §6.5: past_due keeps the badge, grace and expired lose it, a comp
    looks like any other tier."""

    def setUp(self):
        super().setUp()
        from directory.models import Category
        from osds.tenancy import tenant_context
        from tenants.models import InstallSetup

        InstallSetup.objects.create(token_hash="x" * 64, completed_at=T0)
        self.cat = Category.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="plumbers", name="Plumbers"
        )
        with tenant_context(self.tenant):
            self.listing.categories.add(self.cat)
        self.client = Client()

    def page(self):
        return self.client.get("/plumbers/acme-co", HTTP_HOST="acme.test")

    def test_the_badge_follows_the_status(self):
        self.assertNotContains(self.page(), 'class="badge"')
        self.start()
        self.assertContains(self.page(), "Featured</span>")
        self.apply(Trigger.PAYMENT_FAILED, failure_code="x")
        self.assertContains(self.page(), "Featured</span>")  # past_due: full perks
        self.apply(Trigger.DUNNING_ELAPSED, now=T0 + DUNNING)
        self.assertNotContains(self.page(), 'class="badge"')  # grace: perks withdrawn
        self.apply(Trigger.PAYMENT_SUCCEEDED, now=T0 + DUNNING + DAY, period_end=T0 + 90 * DAY)
        self.assertContains(self.page(), "Featured</span>")
        self.apply(Trigger.REFUND)
        self.assertNotContains(self.page(), 'class="badge"')
        self.assertEqual(self.page().status_code, 200)  # never unpublished

    def test_a_comp_shows_a_normal_badge_and_nothing_says_comp(self):
        self.apply(Trigger.GRANT, tier=self.featured, operator=self.admin, reason="secret partner deal")
        page = self.page()
        self.assertContains(page, "Featured</span>")
        self.assertNotContains(page, "comp")
        self.assertNotContains(page, "secret partner deal")

    def test_a_canceled_listing_keeps_its_badge_until_the_period_ends(self):
        self.start()
        self.apply(Trigger.CANCEL, canceled_by="owner")
        self.assertContains(self.page(), "Featured</span>")
