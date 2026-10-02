"""The operator's entitlement commands and pages: comp, revoke, override
(spec §6.8, §4.4; decisions.md §4.11). Managers and above."""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, override_settings
from django.utils import timezone

from audit.models import CommandLog
from billing import commands, machine
from billing.models import Entitlement
from billing.tests.base import BillingTransactionTestCase, Role
from osds.tenancy import tenant_context
from tenants.models import InstallSetup, Tenant

S = Entitlement.Status


class CommandTests(BillingTransactionTestCase):
    def setUp(self):
        super().setUp()
        self.manager = self.operator(Role.MANAGER)

    def grant(self, operator=None, **kw):
        kw.setdefault("tier_key", "featured")
        kw.setdefault("reason", "launch partner")
        with tenant_context(self.tenant):
            return commands.grant_comp(
                self.tenant, listing=self.listing, operator=operator or self.manager, **kw
            )

    def last_log(self, command):
        return CommandLog.objects.filter(command=command).order_by("id").last()

    def test_a_manager_grants_a_comp_and_the_badge_follows(self):
        self.grant()
        ent = self.ent()
        self.assertEqual((ent.status, ent.comp_reason), (S.COMPED, "launch partner"))
        self.assertEqual(self.fresh().current_tier, self.featured)
        log = self.last_log("entitlement.grant")
        self.assertEqual(log.outcome, "applied")
        self.assertEqual(log.actor["type"], "staff")
        self.assertEqual(log.payload, {"listing_id": self.listing.public_id, "tier": "featured", "expires": False})
        self.assertNotIn("launch partner", str(log.payload))
        self.assertEqual(self.events("entitlement.overridden").get().data["admin_id"], self.manager.public_id)

    def test_an_admin_is_an_admin_actor(self):
        self.grant(operator=self.operator(Role.ADMIN))
        self.assertEqual(self.last_log("entitlement.grant").actor["type"], "admin")

    def test_below_manager_is_blocked_and_logged_blocked(self):
        for role in (Role.SUPPORT, Role.MODERATOR, Role.EDITOR):
            with self.subTest(role=role):
                with self.assertRaises(commands.EntitlementRefused) as cm:
                    self.grant(operator=self.operator(role))
                self.assertEqual(cm.exception.reason, "forbidden")
                self.assertEqual(self.last_log("entitlement.grant").outcome, "blocked")
        self.assertFalse(Entitlement.all_tenants.exists())

    def test_an_operator_of_another_tenant_is_blocked(self):
        other = Tenant.objects.create(slug="o", name="O")
        stranger = self.operator(Role.ADMIN, tenant=other)
        with self.assertRaises(commands.EntitlementRefused) as cm:
            self.grant(operator=stranger)
        self.assertEqual(cm.exception.reason, "forbidden")

    def test_guards(self):
        cases = [
            ({"reason": "  "}, "reason_required"),
            ({"tier_key": "nope"}, "tier_unknown"),
            ({"tier_key": ""}, "tier_unknown"),
            ({"expires_at": timezone.now() - timedelta(days=1)}, "expiry_in_past"),
        ]
        for kw, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(commands.EntitlementRefused) as cm:
                    self.grant(**kw)
                self.assertEqual(cm.exception.reason, reason)
                self.assertEqual(self.last_log("entitlement.grant").outcome, "rejected")
        self.assertFalse(Entitlement.all_tenants.exists())

    def test_slot_backed_tiers_cannot_be_comped_yet(self):
        from billing.models import Tier

        Tier.all_tenants.filter(pk=self.featured.pk).update(uses_slot=True)
        with self.assertRaises(commands.EntitlementRefused) as cm:
            self.grant()
        self.assertEqual(cm.exception.reason, "tier_slot")

    def test_a_comp_with_an_expiry(self):
        end = timezone.now() + timedelta(days=30)
        self.grant(expires_at=end)
        self.assertEqual(self.ent().comp_expires_at, end)
        self.assertTrue(self.last_log("entitlement.grant").payload["expires"])

    def test_revoke_ends_the_comp(self):
        self.grant()
        with tenant_context(self.tenant):
            commands.revoke_comp(self.tenant, listing=self.listing, operator=self.manager, reason="ended")
        self.assertEqual(self.ent().status, S.EXPIRED)
        self.assertEqual(self.fresh().current_tier, self.free)
        self.assertEqual(self.last_log("entitlement.revoke").outcome, "applied")

    def test_revoke_needs_a_comp_and_a_reason(self):
        with tenant_context(self.tenant):
            with self.assertRaises(commands.EntitlementRefused) as cm:
                commands.revoke_comp(self.tenant, listing=self.listing, operator=self.manager, reason="x")
            self.assertEqual(cm.exception.reason, "not_comped")
            self.apply(machine.Trigger.START, tier=self.featured, period_end=timezone.now() + timedelta(days=9))
            with self.assertRaises(commands.EntitlementRefused) as cm:
                commands.revoke_comp(self.tenant, listing=self.listing, operator=self.manager, reason="x")
            self.assertEqual(cm.exception.reason, "not_comped")
            self.grant()
            with self.assertRaises(commands.EntitlementRefused) as cm:
                commands.revoke_comp(self.tenant, listing=self.listing, operator=self.manager, reason="")
            self.assertEqual(cm.exception.reason, "reason_required")

    def override(self, **kw):
        kw.setdefault("to_status", "active")
        kw.setdefault("reason", "payment provider error")
        kw.setdefault("tier_key", "featured")
        kw.setdefault("period_end", timezone.now() + timedelta(days=30))
        with tenant_context(self.tenant):
            return commands.override_entitlement(
                self.tenant, listing=self.listing, operator=kw.pop("operator", self.manager), **kw
            )

    def test_override_moves_any_status_to_any(self):
        self.override()
        self.assertEqual(self.ent().status, S.ACTIVE)
        self.override(to_status="expired")
        self.assertEqual(self.ent().status, S.EXPIRED)
        self.assertEqual(self.fresh().current_tier, self.free)
        data = self.events("entitlement.overridden").order_by("id").last().data
        self.assertEqual((data["from"], data["to"], data["reason"]), ("active", "expired", "payment provider error"))

    def test_override_guards(self):
        with self.assertRaises(commands.EntitlementRefused) as cm:
            self.override(reason="")
        self.assertEqual(cm.exception.reason, "reason_required")
        with self.assertRaises(commands.EntitlementRefused) as cm:
            self.override(to_status="comped")  # a comp is granted, not overridden
        self.assertEqual(cm.exception.reason, "invalid_state")
        with self.assertRaises(commands.EntitlementRefused) as cm:
            self.override(to_status="nonsense")
        self.assertEqual(cm.exception.reason, "bad_parameters")
        with self.assertRaises(commands.EntitlementRefused) as cm:
            self.override(tier_key="")  # no entitlement yet, so a tier is needed
        self.assertEqual(cm.exception.reason, "bad_parameters")
        with self.assertRaises(commands.EntitlementRefused) as cm:
            self.override(to_status="trialing")
        self.assertEqual(cm.exception.reason, "bad_parameters")
        self.assertFalse(Entitlement.all_tenants.exists())

    def test_override_to_past_due_and_grace_arm_their_timers(self):
        self.override(to_status="past_due")
        self.assertIsNotNone(self.ent().dunning_ends_at)
        self.override(to_status="grace")
        self.assertIsNotNone(self.ent().grace_ends_at)

    def test_override_below_manager_is_blocked(self):
        with self.assertRaises(commands.EntitlementRefused) as cm:
            self.override(operator=self.operator(Role.EDITOR))
        self.assertEqual(cm.exception.reason, "forbidden")
        self.assertEqual(self.last_log("entitlement.override").outcome, "blocked")

    def test_commands_refuse_to_run_inside_a_transaction(self):
        from django.db import transaction

        from audit.command_log import MustNotBeInTransaction

        with transaction.atomic(), tenant_context(self.tenant):
            with self.assertRaises(MustNotBeInTransaction):
                commands.grant_comp(self.tenant, listing=self.listing, operator=self.manager,
                                    tier_key="featured", reason="x")


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test",
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class EntitlementPageTests(BillingTransactionTestCase):
    def setUp(self):
        super().setUp()
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())

    def client_for(self, role):
        c = Client()
        c.force_login(self.operator(role))
        return c

    def get(self, c, path):
        return c.get(path, HTTP_HOST="acme.test")

    def post(self, c, path, data=None, **kw):
        return c.post(path, data or {}, HTTP_HOST="acme.test", **kw)

    def grant_via_page(self, c, **extra):
        data = {"listing": self.listing.public_id, "tier": "featured", "reason": "partner", **extra}
        return self.post(c, "/admin/entitlements/grant/", data)

    def test_only_managers_and_above_reach_the_pages(self):
        for role in (Role.SUPPORT, Role.MODERATOR, Role.EDITOR):
            with self.subTest(role=role):
                c = self.client_for(role)
                self.assertEqual(self.get(c, "/admin/entitlements/").status_code, 403)
                self.assertEqual(self.grant_via_page(c).status_code, 403)
        self.assertFalse(Entitlement.all_tenants.exists())
        self.assertEqual(self.get(self.client_for(Role.MANAGER), "/admin/entitlements/").status_code, 200)

    def test_grant_then_detail_then_revoke(self):
        c = self.client_for(Role.MANAGER)
        r = self.grant_via_page(c)
        ent = self.ent()
        self.assertEqual((r.status_code, r["Location"]), (302, f"/admin/entitlements/{ent.public_id}/"))
        page = self.get(c, r["Location"])
        self.assertContains(page, "Acme Co")
        self.assertContains(page, "partner")
        self.assertContains(page, "entitlement.overridden")
        self.assertContains(page, "listing.tier_changed")
        self.assertContains(page, "Revoke the comp")
        self.post(c, f"/admin/entitlements/{ent.public_id}/revoke/", {"reason": "done"})
        self.assertEqual(self.ent().status, S.EXPIRED)
        self.assertNotContains(self.get(c, r["Location"]), "Revoke the comp")

    def test_grant_refusals_show_a_message(self):
        c = self.client_for(Role.MANAGER)
        r = self.post(c, "/admin/entitlements/grant/",
                      {"listing": "listing_NOPE", "tier": "featured", "reason": "x"}, follow=True)
        self.assertContains(r, "No such listing")
        r = self.post(c, "/admin/entitlements/grant/",
                      {"listing": self.listing.public_id, "tier": "featured", "reason": ""}, follow=True)
        self.assertContains(r, "Fill in the listing, tier and reason")
        self.assertFalse(Entitlement.all_tenants.exists())

    def test_override_through_the_page(self):
        c = self.client_for(Role.MANAGER)
        self.grant_via_page(c)
        ent = self.ent()
        r = self.post(c, f"/admin/entitlements/{ent.public_id}/override/",
                      {"to_status": "expired", "reason": "mistake"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.ent().status, S.EXPIRED)

    def test_the_list_filters_by_status(self):
        c = self.client_for(Role.MANAGER)
        self.grant_via_page(c)
        self.assertContains(self.get(c, "/admin/entitlements/?status=comped"), "Acme Co")
        self.assertNotContains(self.get(c, "/admin/entitlements/?status=active"), "Acme Co")

    def test_an_entitlement_of_another_tenant_is_404(self):
        from billing.models import Tier
        from directory.models import Listing, ListingType

        other = Tenant.objects.create(slug="o", name="O", primary_domain="o.test")
        lt = ListingType.all_tenants.create(tenant=other, key="b", label_singular="B", label_plural="Bs", path_segment="bs")
        tier = Tier.all_tenants.create(tenant=other, key="t", name="T", rank=1)
        listing = Listing.all_tenants.create(tenant=other, listing_type=lt, slug="x", name="X")
        foreign = Entitlement.all_tenants.create(tenant=other, listing=listing, tier=tier, status=S.ACTIVE)
        c = self.client_for(Role.MANAGER)
        self.assertEqual(self.get(c, f"/admin/entitlements/{foreign.public_id}/").status_code, 404)
        self.assertEqual(self.post(c, f"/admin/entitlements/{foreign.public_id}/revoke/", {"reason": "x"}).status_code, 404)
        self.assertEqual(foreign.status, S.ACTIVE)

    def test_action_routes_are_post_only_and_csrf_protected(self):
        c = self.client_for(Role.MANAGER)
        self.assertEqual(self.get(c, "/admin/entitlements/grant/").status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.cookies = c.cookies
        self.assertEqual(self.grant_via_page(strict).status_code, 403)
