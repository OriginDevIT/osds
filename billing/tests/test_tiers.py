"""Tier configuration: the service and the admin pages (spec §4.2, §6.4;
decisions.md §4.11). Tiers and pricing are the admin's (spec §4.4)."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import Client, override_settings
from django.utils import timezone

from audit.models import OutboxEvent
from billing import machine, tiers
from billing.models import Entitlement, Tier
from billing.tests.base import T0, BillingTestCase, BillingTransactionTestCase, Role
from osds.tenancy import tenant_context
from tenants.models import InstallSetup


class TierServiceTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.operator(Role.ADMIN)

    def create(self, **kw):
        kw.setdefault("key", "gold")
        kw.setdefault("name", "Gold")
        kw.setdefault("rank", 3)
        with tenant_context(self.tenant):
            return tiers.create_tier(self.tenant, actor=self.admin, **kw)

    def settings_events(self):
        return OutboxEvent.all_tenants.filter(type="tenant.settings_changed", tenant=self.tenant).order_by("id")

    def test_create_stores_the_tier_and_emits_a_settings_change(self):
        before = self.settings_events().count()
        tier = self.create(purchasable=True, price_minor=4900, currency="usd", interval="year",
                           trial_days=14, badge_label="Gold", perks={"priority": True})
        self.assertEqual((tier.currency, tier.interval, tier.trial_days), ("USD", "year", 14))
        self.assertEqual(self.settings_events().count(), before + 1)
        change = self.settings_events().last().data["changes"][0]
        self.assertEqual((change["op"], change["path"]), ("add", "/tiers/gold"))
        self.assertEqual(change["value"]["price_minor"], 4900)
        self.assertEqual(self.settings_events().last().actor["id"], self.admin.public_id)

    def test_rules(self):
        bad = [
            {"key": "Bad Key!"},
            {"name": " "},
            {"rank": -1},
            {"uses_slot": True},
            {"perks": []},
            {"purchasable": True},  # no price
            {"purchasable": True, "price_minor": 100, "currency": "us", "interval": "month"},
            {"purchasable": True, "price_minor": 100, "currency": "USD"},  # no interval
            {"purchasable": True, "price_minor": 100, "currency": "USD", "interval": "month", "trial_days": 0},
            {"purchasable": True, "price_minor": 100, "currency": "USD", "interval": "month", "trial_days": 91},
            {"rank": 0, "key": "free2", "purchasable": True, "price_minor": 100, "currency": "USD", "interval": "month"},
            {"price_minor": 100},  # a free tier has no price
        ]
        for kw in bad:
            with self.subTest(kw=kw):
                with self.assertRaises(tiers.TierError):
                    self.create(**{"key": "x1", "rank": 9, **kw})
        self.assertFalse(Tier.all_tenants.filter(key__in=["x1", "free2"]).exists())

    def test_key_and_rank_are_unique_per_tenant(self):
        with self.assertRaises(tiers.TierError):
            self.create(key="featured", rank=9)
        with self.assertRaises(tiers.TierError):
            self.create(key="other", rank=2)

    def test_the_database_refuses_a_purchasable_rank_zero_and_a_priceless_sale(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Tier.all_tenants.create(tenant=self.tenant, key="z", name="Z", rank=0,
                                    purchasable=True, price_minor=1, currency="USD", interval="month")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Tier.all_tenants.create(tenant=self.tenant, key="y", name="Y", rank=5, purchasable=True)

    def test_update_changes_fields_and_emits_only_when_something_changed(self):
        before = self.settings_events().count()
        tiers.update_tier(self.featured, actor=self.admin, price_minor=3900)
        self.assertEqual(Tier.all_tenants.get(pk=self.featured.pk).price_minor, 3900)
        self.assertEqual(self.settings_events().count(), before + 1)
        self.assertEqual(self.settings_events().last().data["changes"][0]["op"], "replace")
        tiers.update_tier(Tier.all_tenants.get(pk=self.featured.pk), actor=self.admin, price_minor=3900)
        self.assertEqual(self.settings_events().count(), before + 1)

    def test_the_key_is_the_identity(self):
        with self.assertRaises(tiers.TierError):
            tiers.update_tier(self.featured, actor=self.admin, key="renamed")
        with self.assertRaises(tiers.TierError):
            tiers.update_tier(self.featured, actor=self.admin, bogus=1)

    def test_a_purchasable_tier_cannot_be_turned_into_rank_zero(self):
        with self.assertRaises(tiers.TierError):
            tiers.update_tier(self.featured, actor=self.admin, rank=0)

    def test_delete_is_refused_while_anything_uses_the_tier(self):
        self.apply(machine.Trigger.START, tier=self.featured, period_end=T0 + machine.DUNNING)
        with tenant_context(self.tenant), self.assertRaises(tiers.TierError):
            tiers.delete_tier(self.featured, actor=self.admin)
        other = self.make_listing("shows", "Shows")
        from directory.models import Listing

        Listing.all_tenants.filter(pk=other.pk).update(current_tier=self.verified)
        with tenant_context(self.tenant), self.assertRaises(tiers.TierError):
            tiers.delete_tier(self.verified, actor=self.admin)

    def test_delete_removes_an_unused_tier_and_says_so(self):
        gold = self.create()
        with tenant_context(self.tenant):
            tiers.delete_tier(gold, actor=self.admin)
        self.assertFalse(Tier.all_tenants.filter(key="gold").exists())
        self.assertEqual(self.settings_events().last().data["changes"][0]["op"], "remove")

    def test_tiers_are_per_tenant(self):
        from tenants.models import Tenant

        other = Tenant.objects.create(slug="o", name="O")
        with tenant_context(other):
            tiers.create_tier(other, key="featured", name="Theirs", rank=2, actor=self.admin)
        self.assertEqual(Tier.all_tenants.filter(key="featured").count(), 2)


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test",
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class TierPageTests(BillingTransactionTestCase):
    def setUp(self):
        super().setUp()
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())

    def client_for(self, role):
        c = Client()
        c.force_login(self.operator(role))
        return c

    def get(self, c, path):
        return c.get(path, HTTP_HOST="acme.test")

    def post(self, c, path, data=None):
        return c.post(path, data or {}, HTTP_HOST="acme.test")

    def test_only_the_admin_reaches_tier_pages(self):
        for role in (Role.SUPPORT, Role.MODERATOR, Role.EDITOR, Role.MANAGER):
            with self.subTest(role=role):
                c = self.client_for(role)
                self.assertEqual(self.get(c, "/admin/tiers/").status_code, 403)
                self.assertEqual(self.post(c, "/admin/tiers/new/", {"key": "k"}).status_code, 403)
        self.assertEqual(self.get(self.client_for(Role.ADMIN), "/admin/tiers/").status_code, 200)

    def test_the_list_shows_the_tiers_and_no_warning_when_there_is_a_rank_zero(self):
        r = self.get(self.client_for(Role.ADMIN), "/admin/tiers/")
        self.assertContains(r, "Featured")
        self.assertContains(r, "2900 USD / month")
        self.assertNotContains(r, "no rank-0 tier")

    def test_the_list_warns_when_there_is_no_rank_zero_tier(self):
        Tier.all_tenants.filter(rank=0).delete()
        r = self.get(self.client_for(Role.ADMIN), "/admin/tiers/")
        self.assertContains(r, "no rank-0 tier")
        self.assertContains(r, "hidden")

    def test_create_edit_and_delete_through_the_pages(self):
        c = self.client_for(Role.ADMIN)
        r = self.post(c, "/admin/tiers/new/", {"key": "gold", "name": "Gold", "rank": "3",
                                                "purchasable": "on", "price_minor": "4900",
                                                "currency": "usd", "interval": "month"})
        self.assertEqual(r.status_code, 302)
        gold = Tier.all_tenants.get(key="gold")
        self.assertEqual((gold.currency, gold.price_minor), ("USD", 4900))
        r = self.post(c, "/admin/tiers/gold/", {"key": "gold", "name": "Gold+", "rank": "3",
                                                "purchasable": "on", "price_minor": "5900",
                                                "currency": "USD", "interval": "month"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Tier.all_tenants.get(key="gold").name, "Gold+")
        self.assertEqual(self.post(c, "/admin/tiers/gold/delete/").status_code, 302)
        self.assertFalse(Tier.all_tenants.filter(key="gold").exists())

    def test_a_refused_tier_shows_the_reason_and_saves_nothing(self):
        c = self.client_for(Role.ADMIN)
        r = self.post(c, "/admin/tiers/new/", {"key": "paid0", "name": "P", "rank": "0",
                                                "purchasable": "on", "price_minor": "100",
                                                "currency": "USD", "interval": "month"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "rank-0 tier is the free fallback")
        self.assertFalse(Tier.all_tenants.filter(key="paid0").exists())

    def test_bad_perks_json_is_a_form_error(self):
        c = self.client_for(Role.ADMIN)
        r = self.post(c, "/admin/tiers/new/", {"key": "k", "name": "K", "rank": "4", "perks": "{nope"})
        self.assertContains(r, "valid JSON")

    def test_a_tier_in_use_cannot_be_deleted_from_the_page(self):
        self.apply(machine.Trigger.START, tier=self.featured, period_end=T0 + machine.DUNNING)
        c = self.client_for(Role.ADMIN)
        r = c.post("/admin/tiers/featured/delete/", {}, HTTP_HOST="acme.test", follow=True)
        self.assertContains(r, "cannot be deleted")
        self.assertTrue(Tier.all_tenants.filter(key="featured").exists())

    def test_delete_is_post_only_and_another_tenants_tier_is_404(self):
        c = self.client_for(Role.ADMIN)
        self.assertEqual(self.get(c, "/admin/tiers/featured/delete/").status_code, 405)
        self.assertEqual(self.get(c, "/admin/tiers/nope/").status_code, 404)

    def test_the_admin_home_links_by_role(self):
        self.assertContains(self.get(self.client_for(Role.ADMIN), "/admin/"), "Tiers")
        manager = self.get(self.client_for(Role.MANAGER), "/admin/")
        self.assertContains(manager, "Entitlements and comps")
        self.assertNotContains(manager, ">Tiers<")
        self.assertNotContains(self.get(self.client_for(Role.EDITOR), "/admin/"), "Entitlements and comps")
        self.assertIsNotNone(Entitlement)
