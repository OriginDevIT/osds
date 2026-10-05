"""Operator settings saves in the command log (#225, decisions.md §4): the mail
page and the wizard's domain, storage, mail-skip and claims steps. One
``settings.update`` row per successful save; the payload names the page and the
changed field names and never a value."""

from __future__ import annotations

import functools

from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse as _reverse
from django.utils import timezone

from audit.tests.settings_log import assert_one_save, settings_rows
from osds.tests.mail_stub import email_send_stub
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant
from tenants.services import InvalidTenantSettings, update_mail_settings
from tenants.tests.test_wizard import TOKEN, WizardBase

reverse = functools.partial(_reverse, urlconf="osds.urls_tenant")
HOST = "acme.test"
PASSWORD = "hunter2-PASSWORD"
MAIL = {
    "host": "smtp.example.test", "port": "587", "security": "starttls",
    "from_email": "noreply@acme.test", "username": "bot", "password": PASSWORD,
}


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test",
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MailPageTests(TransactionTestCase):
    def setUp(self):
        self.enterContext(email_send_stub(available=False))
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(slug="acme", name="Acme", primary_domain=HOST)
        self.admin = Operator.objects.create_user(email="admin@acme.test", password="pw")
        StaffMembership.objects.create(
            operator=self.admin, tenant=self.tenant, role=StaffMembership.Role.ADMIN,
            status=StaffMembership.Status.ACTIVE,
        )
        self.client = Client()
        self.client.force_login(self.admin)
        self.url = reverse("directory_admin:mail-settings")

    def save(self, **over):
        data = {**MAIL, **over}
        return self.client.post(self.url, {k: v for k, v in data.items() if v is not None}, HTTP_HOST=HOST)

    def test_a_save_writes_one_row_naming_the_fields_and_no_value(self):
        self.assertEqual(self.save().status_code, 302)
        assert_one_save(
            self, self.tenant, self.admin, page="mail",
            fields=["host", "port", "security", "from_email", "username", "password"],
            forbidden=[PASSWORD, "smtp.example.test", "noreply@acme.test", "bot"],
        )

    def test_only_the_fields_that_changed_are_named(self):
        self.save()
        self.save(port="2525", password="")
        rows = settings_rows(self.tenant)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].payload["fields"], ["port"])

    def test_a_form_refused_save_attempts_no_command_and_writes_no_row(self):
        self.save()
        r = self.save(host="other.example.test", password="")  # host change needs the password
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x.outcome for x in settings_rows(self.tenant)], ["applied"])

    def test_a_service_refusal_is_logged_rejected_with_a_fixed_reason(self):
        self.save()
        with self.assertRaises(InvalidTenantSettings):
            update_mail_settings(
                tenant=Tenant.objects.get(pk=self.tenant.pk),
                config={"host": "other.example.test", "port": 587, "security": "starttls",
                        "from_email": "noreply@acme.test", "username": "bot"},
                changed_by=self.admin,
            )
        row = settings_rows(self.tenant)[-1]
        self.assertEqual((row.outcome, row.problem), ("rejected", {"reason": "invalid"}))
        self.assertNotIn("other.example.test", str([row.payload, row.problem]))


@override_settings(ALLOWED_HOSTS=["*"])
class WizardSettingsTests(WizardBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.post("/setup/unlock/", {"token": TOKEN})
        self.do_account(self.client)
        self.do_directory(self.client)
        self.tenant = Tenant.objects.get()
        self.operator = Operator.objects.get()

    def test_domain_step(self):
        self.client.post("/setup/domain/", {"domain": "directory.example.test", "action": "skip"})
        assert_one_save(
            self, self.tenant, self.operator, page="domain", fields=["domain"],
            forbidden=["directory.example.test"],
        )

    def test_storage_step_names_the_secret_key_and_never_its_value(self):
        self.do_domain(self.client)
        before = len(settings_rows(self.tenant))
        self.client.post("/setup/storage/", {
            "backend": "s3", "bucket": "my-bucket", "endpoint": "https://s3.example.test",
            "access_key": "AKIAEXAMPLE", "secret_key": "s3-SECRET-VALUE",
        })
        assert_one_save(
            self, self.tenant, self.operator, page="storage", since=before,
            fields=["backend", "bucket", "endpoint", "access_key", "secret_key"],
            forbidden=["s3-SECRET-VALUE", "AKIAEXAMPLE", "my-bucket", "s3.example.test"],
        )

    def test_skipping_mail(self):
        self.do_domain(self.client)
        self.client.post("/setup/storage/", {"backend": "local"})
        before = len(settings_rows(self.tenant))
        self.client.post("/setup/smtp/", {"action": "skip"})
        assert_one_save(self, self.tenant, self.operator, page="mail", fields=[], since=before)

    def test_claims_step(self):
        self.do_domain(self.client)
        self.client.post("/setup/storage/", {"backend": "local"})
        self.client.post("/setup/smtp/", {"action": "skip"})
        before = len(settings_rows(self.tenant))
        self.client.post("/setup/claims/", {"methods": ["domain_email"], "domain_email_ttl_minutes": "1440"})
        assert_one_save(
            self, self.tenant, self.operator, page="claims",
            fields=["enabled_methods", "ttl"], since=before, forbidden=["domain_email", "1440"],
        )
