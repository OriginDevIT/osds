"""The wizard's mail step (#121, decisions.md §4.5): the shared form, Skip,
and the legacy ``use_tls`` field being gone."""

from __future__ import annotations

from django.test import Client

from audit.models import OutboxEvent
from tenants.models import InstallSetup, Tenant
from tenants.secrets import has_secret
from tenants.tests.test_wizard import WizardBase, _final_path


class WizardMailStepTests(WizardBase):
    def reach_smtp(self) -> Client:
        client = Client()
        self.unlock(client)
        self.do_account(client)
        self.do_directory(client)
        self.do_domain(client)
        client.post("/setup/storage/", {"backend": "local"})
        return client

    def test_the_step_renders_the_shared_form_with_skip_and_a_prefill(self):
        client = self.reach_smtp()
        r = client.get("/setup/smtp/")
        self.assertContains(r, 'name="security"')
        self.assertContains(r, "noreply@directory.example.test")
        self.assertContains(r, 'value="skip"')
        self.assertNotContains(r, "use_tls")

    def test_saving_stores_security_not_use_tls(self):
        client = self.reach_smtp()
        client.post(
            "/setup/smtp/",
            {"host": "smtp.example.test", "port": "465", "security": "tls",
             "from_email": "noreply@directory.example.test", "username": "bot",
             "password": "pw"},
        )
        smtp = Tenant.objects.get().settings["smtp"]
        self.assertEqual(smtp["security"], "tls")
        self.assertNotIn("use_tls", smtp)
        self.assertTrue(has_secret("smtp_password", tenant=Tenant.objects.get()))
        self.assertEqual(_final_path(client.get("/setup/", follow=True)), "/setup/claims/")

    def test_username_with_security_none_rerenders_and_does_not_advance(self):
        client = self.reach_smtp()
        r = client.post(
            "/setup/smtp/",
            {"host": "h.example.test", "port": "25", "security": "none",
             "from_email": "a@b.test", "username": "bot"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("smtp", Tenant.objects.get().settings)
        self.assertEqual(_final_path(client.get("/setup/", follow=True)), "/setup/smtp/")

    def test_skip_stores_an_empty_block_and_advances(self):
        client = self.reach_smtp()
        r = client.post("/setup/smtp/", {"action": "skip"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Tenant.objects.get().settings["smtp"], {})
        self.assertEqual(_final_path(client.get("/setup/", follow=True)), "/setup/claims/")
        self.assertTrue(
            OutboxEvent.all_tenants.filter(type="tenant.settings_changed").exists()
        )

    def test_skip_ignores_a_half_filled_form(self):
        client = self.reach_smtp()
        client.post("/setup/smtp/", {"action": "skip", "host": "", "port": "abc"})
        self.assertEqual(Tenant.objects.get().settings["smtp"], {})

    def test_setup_completes_after_skipping_mail(self):
        client = self.reach_smtp()
        client.post("/setup/smtp/", {"action": "skip"})
        client.post(
            "/setup/claims/",
            {"methods": ["manual"], "domain_email_ttl_minutes": "1440"},
        )
        self.assertEqual(_final_path(client.get("/setup/", follow=True)), "/setup/done/")
        client.post("/setup/done/", {})
        self.assertIsNotNone(InstallSetup.load().completed_at)
