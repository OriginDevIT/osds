"""The generic adapter-settings page and service (decisions.md §4.11): core
renders the provider's own field list and never names the provider."""

from __future__ import annotations

from django.test import Client, override_settings
from django.utils import timezone

from audit.models import OutboxEvent
from billing import settings_service
from billing.tests.base import Role
from billing.tests.payments_base import ADAPTER_ID, PaymentTestCase
from osds.adapters import override_capability
from tenants import services as tenant_services
from tenants.models import InstallSetup, Secret, Tenant
from tenants.secrets import get_secret, has_secret

URL = "/admin/settings/payments/"


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test",
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PaymentSettingsTests(PaymentTestCase):
    def setUp(self):
        super().setUp()
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.admin = self.operator(Role.ADMIN)

    def client_for(self, op):
        c = Client()
        c.force_login(op)
        return c

    def get(self, op=None):
        return self.client_for(op or self.admin).get(URL, HTTP_HOST="acme.test")

    def post(self, data, op=None):
        return self.client_for(op or self.admin).post(URL, data, HTTP_HOST="acme.test")

    def test_only_the_admin_sees_it(self):
        for role in (Role.SUPPORT, Role.MODERATOR, Role.EDITOR, Role.MANAGER):
            with self.subTest(role=role):
                self.assertEqual(self.get(self.operator(role)).status_code, 403)
                self.assertEqual(self.post({"account": "x"}, self.operator(role)).status_code, 403)
        self.assertEqual(Client().get(URL, HTTP_HOST="acme.test").status_code, 302)

    def test_the_form_is_built_from_the_providers_field_list(self):
        page = self.get()
        self.assertContains(page, 'name="account"')
        self.assertContains(page, 'name="api_key"')
        self.assertContains(page, 'type="password"')
        self.assertContains(page, "clear_api_key")
        self.assertContains(page, ADAPTER_ID)
        self.assertContains(page, f"https://acme.test/_adapters/{ADAPTER_ID}/inbound/")

    def test_no_provider_says_so(self):
        with override_capability({}):
            self.assertContains(self.get(), "No payment provider is installed")

    def test_saving_stores_config_as_settings_and_secrets_as_secrets(self):
        before = OutboxEvent.all_tenants.filter(type="tenant.settings_changed").count()
        r = self.post({"account": "acct_1", "api_key": "sk_live_SECRET", "webhook_secret": "whsec_x"})
        self.assertEqual(r.status_code, 302)
        tenant = Tenant.objects.get(pk=self.tenant.pk)
        self.assertEqual(tenant.settings["adapters"][ADAPTER_ID], {"account": "acct_1"})
        self.assertEqual(get_secret(f"{ADAPTER_ID}_api_key", tenant=tenant), "sk_live_SECRET")
        self.assertEqual(get_secret(f"{ADAPTER_ID}_webhook_secret", tenant=tenant), "whsec_x")
        events = OutboxEvent.all_tenants.filter(type="tenant.settings_changed").order_by("id")
        self.assertEqual(events.count(), before + 1)
        blob = str(events.last().data)
        self.assertNotIn("SECRET", blob)
        self.assertNotIn("whsec", blob)
        self.assertIn("acct_1", blob)

    def test_the_secret_is_never_rendered_back(self):
        self.post({"account": "a", "api_key": "sk_live_SECRET"})
        page = self.get()
        self.assertNotContains(page, "sk_live_SECRET")
        self.assertContains(page, "A value is stored")
        failed = self.post({"account": "a", "api_key": "TYPED-SECRET", "clear_api_key": "on"})
        self.assertNotContains(failed, "TYPED-SECRET")

    def test_a_blank_secret_keeps_the_stored_one_and_clear_removes_it(self):
        self.post({"account": "a", "api_key": "k1", "webhook_secret": "w1"})
        self.post({"account": "b", "api_key": "", "webhook_secret": ""})
        self.assertEqual(get_secret(f"{ADAPTER_ID}_api_key", tenant=self.tenant), "k1")
        self.post({"account": "b", "api_key": "k2", "webhook_secret": "", "clear_webhook_secret": "on"})
        self.assertEqual(get_secret(f"{ADAPTER_ID}_api_key", tenant=self.tenant), "k2")
        self.assertFalse(has_secret(f"{ADAPTER_ID}_webhook_secret", tenant=self.tenant))

    def test_a_required_secret_cannot_be_left_unset(self):
        r = self.post({"account": "a", "api_key": ""})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "API key is required")
        self.assertNotIn("adapters", Tenant.objects.get(pk=self.tenant.pk).settings)
        self.post({"account": "a", "api_key": "k1"})
        self.assertEqual(self.post({"account": "a", "api_key": "", "clear_api_key": "on"}).status_code, 200)
        self.assertTrue(has_secret(f"{ADAPTER_ID}_api_key", tenant=self.tenant))  # the refusal rolled back

    def test_a_rejected_save_leaves_no_secret_behind(self):
        # api_key is required and missing, but a webhook secret was typed.
        self.post({"account": "a", "api_key": "", "webhook_secret": "whsec"})
        self.assertFalse(Secret.objects.filter(key__startswith=ADAPTER_ID).exists())

    def test_clearing_a_config_value(self):
        self.post({"account": "a", "api_key": "k"})
        self.post({"account": "", "api_key": ""})
        self.assertEqual(Tenant.objects.get(pk=self.tenant.pk).settings["adapters"][ADAPTER_ID], {})

    def test_the_page_says_when_payments_are_available_and_when_not(self):
        self.assertContains(self.get(), "available")
        self.provider._available = False
        self.assertContains(self.get(), "not available")
        Tenant.objects.filter(pk=self.tenant.pk).update(domain_verified_at=None)
        self.provider._available = True
        page = self.get()
        self.assertContains(page, "needs a verified domain")
        self.assertNotContains(page, "/_adapters/")

    def test_another_adapters_config_is_left_alone(self):
        tenant_services.update_tenant_settings(
            tenant=self.tenant, changes={"adapters": {"other": {"x": "1"}}}, changed_by=self.admin
        )
        self.post({"account": "a", "api_key": "k"})
        tenant = Tenant.objects.get(pk=self.tenant.pk)
        self.assertEqual(tenant.settings["adapters"]["other"], {"x": "1"})
        self.assertEqual(tenant.settings["adapters"][ADAPTER_ID], {"account": "a"})

    def test_the_service_refuses_an_unknown_setting(self):
        with self.assertRaises(settings_service.AdapterSettingsError):
            settings_service.update_adapter_settings(
                self.tenant, provider=self.provider, values={"nope": "x", "api_key": "k"}, changed_by=self.admin
            )

    def test_a_declared_pattern_is_enforced_without_echoing_the_value(self):
        from osds.adapter_api import SettingField

        class Patterned:
            adapter_id = ADAPTER_ID

            def settings_fields(self):
                return [SettingField("token", "Token", secret=True, pattern=r"tok_[a-z]+"),
                        SettingField("ids", "Ids", pattern=r"[a-z]+=[0-9]+")]

        for values in ({"token": "WRONG-SECRET-VALUE"}, {"ids": "oops"}):
            with self.subTest(values=values):
                with self.assertRaises(settings_service.AdapterSettingsError) as cm:
                    settings_service.update_adapter_settings(
                        self.tenant, provider=Patterned(), values=values, changed_by=self.admin)
                self.assertIn("not in the expected format", str(cm.exception))
                self.assertNotIn("WRONG-SECRET-VALUE", str(cm.exception))
        self.assertFalse(Secret.objects.filter(key__startswith=ADAPTER_ID).exists())
        settings_service.update_adapter_settings(
            self.tenant, provider=Patterned(), values={"token": "tok_abc", "ids": "a=1"}, changed_by=self.admin)
        self.assertEqual(get_secret(f"{ADAPTER_ID}_token", tenant=self.tenant), "tok_abc")

    def test_the_admin_home_links_to_it(self):
        home = self.client_for(self.admin).get("/admin/", HTTP_HOST="acme.test")
        self.assertContains(home, "Payment settings")
        self.assertNotContains(self.client_for(self.operator(Role.MANAGER)).get("/admin/", HTTP_HOST="acme.test"),
                               "Payment settings")
