"""First-run wizard: routing, the tenant.created emission, and -- the point --
resuming at the right step after the browser is closed.
"""

from __future__ import annotations

import hashlib

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from audit.models import OutboxEvent
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant

TOKEN = "known-first-run-token"
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
PW = "sup3r-secret-passphrase"


def _final_path(response) -> str:
    return response.request["PATH_INFO"]


class WizardBase(TestCase):
    def setUp(self):
        InstallSetup.objects.create(token_hash=TOKEN_HASH)

    def unlock(self, client: Client) -> None:
        client.post("/setup/unlock/", {"token": TOKEN})

    def do_account(self, client: Client) -> None:
        client.post(
            "/setup/account/",
            {"email": "admin@example.test", "name": "Admin",
             "password1": PW, "password2": PW},
        )

    def do_directory(self, client: Client) -> None:
        client.post(
            "/setup/directory/",
            {"name": "Chicago Plumbers", "slug": "chicago-plumbers", "mode": "single"},
        )

    def do_domain(self, client: Client) -> None:
        client.post(
            "/setup/domain/",
            {"domain": "directory.example.test", "action": "skip"},
        )


class RoutingTests(WizardBase):
    def test_any_path_redirects_into_the_wizard(self):
        response = Client().get("/anything", follow=True)
        self.assertEqual(_final_path(response), "/setup/unlock/")

    def test_wizard_is_locked_until_the_token_is_entered(self):
        client = Client()
        response = client.get("/setup/", follow=True)
        self.assertEqual(_final_path(response), "/setup/unlock/")

    def test_wrong_token_does_not_unlock(self):
        client = Client()
        client.post("/setup/unlock/", {"token": "wrong"})
        self.assertNotIn("setup_unlocked", client.session)

    def test_correct_token_unlocks_and_routes_to_the_first_step(self):
        client = Client()
        self.unlock(client)
        response = client.get("/setup/", follow=True)
        self.assertEqual(_final_path(response), "/setup/account/")

    @override_settings(OSDS_CONSOLE_HOST="testserver")
    def test_wizard_is_gone_once_setup_is_complete(self):
        InstallSetup.objects.filter(pk=1).update(completed_at=timezone.now())
        response = Client().get("/setup/unlock/")
        self.assertEqual(response.status_code, 404)


class HappyPathTests(WizardBase):
    def test_full_run_emits_tenant_created_and_completes(self):
        client = Client()
        self.unlock(client)

        self.do_account(client)
        self.assertEqual(Operator.objects.count(), 1)
        self.assertTrue(Operator.objects.get().is_superadmin)

        self.do_directory(client)
        tenant = Tenant.objects.get()
        self.assertEqual(tenant.slug, "chicago-plumbers")
        self.assertTrue(
            OutboxEvent.all_tenants.filter(
                type="tenant.created", subject=tenant.public_id
            ).exists()
        )
        membership = StaffMembership.objects.get()
        self.assertEqual(membership.status, "active")
        self.assertEqual(membership.role, StaffMembership.Role.ADMIN)
        self.assertEqual(
            set(
                OutboxEvent.all_tenants.filter(
                    subject=membership.operator.public_id
                ).values_list("type", flat=True)
            ),
            {"staff.invited", "staff.accepted"},
        )

        self.do_domain(client)
        tenant.refresh_from_db()
        self.assertEqual(tenant.primary_domain, "directory.example.test")
        self.assertTrue(tenant.settings.get("domain_challenge"))
        self.assertTrue(
            OutboxEvent.all_tenants.filter(type="tenant.settings_changed").exists()
        )

        client.post("/setup/storage/", {"backend": "local"})
        client.post(
            "/setup/smtp/",
            {"host": "localhost", "port": "1025", "from_email": "no@reply.test"},
        )
        client.post(
            "/setup/claims/",
            {"methods": ["manual"], "domain_email_ttl_minutes": "1440"},
        )

        response = client.get("/setup/", follow=True)
        self.assertEqual(_final_path(response), "/setup/done/")
        client.post("/setup/done/", {})
        self.assertIsNotNone(InstallSetup.load().completed_at)

    def test_done_cannot_be_forced_while_a_step_is_unfinished(self):
        client = Client()
        self.unlock(client)
        self.do_account(client)
        self.do_directory(client)
        # domain not set yet
        client.post("/setup/done/", {})
        self.assertIsNone(InstallSetup.load().completed_at)


class ResumeAfterAbandonmentTests(WizardBase):
    def test_resume_at_directory_after_account(self):
        first = Client()
        self.unlock(first)
        self.do_account(first)

        # "closed the browser": a fresh client with no session
        resumed = Client()
        self.assertEqual(
            _final_path(resumed.get("/setup/", follow=True)), "/setup/unlock/"
        )
        self.unlock(resumed)
        self.assertEqual(
            _final_path(resumed.get("/setup/", follow=True)), "/setup/directory/"
        )

    def test_resume_at_domain_after_directory(self):
        first = Client()
        self.unlock(first)
        self.do_account(first)
        self.do_directory(first)

        resumed = Client()
        self.unlock(resumed)
        self.assertEqual(
            _final_path(resumed.get("/setup/", follow=True)), "/setup/domain/"
        )
        # the tenant and its bootstrap membership survived the abandonment
        self.assertEqual(Tenant.objects.count(), 1)
        self.assertEqual(StaffMembership.objects.count(), 1)

    def test_resume_at_storage_after_domain(self):
        first = Client()
        self.unlock(first)
        self.do_account(first)
        self.do_directory(first)
        self.do_domain(first)

        resumed = Client()
        self.unlock(resumed)
        self.assertEqual(
            _final_path(resumed.get("/setup/", follow=True)), "/setup/storage/"
        )

    def test_completed_step_is_not_offered_again(self):
        client = Client()
        self.unlock(client)
        self.do_account(client)
        # account is done; visiting it directly bounces onward
        response = client.get("/setup/account/", follow=True)
        self.assertNotEqual(_final_path(response), "/setup/account/")

    def test_directory_step_does_not_double_create_the_tenant(self):
        client = Client()
        self.unlock(client)
        self.do_account(client)
        self.do_directory(client)
        self.do_directory(client)  # replayed POST
        self.assertEqual(Tenant.objects.count(), 1)
        self.assertEqual(
            OutboxEvent.all_tenants.filter(type="tenant.created").count(), 1
        )
