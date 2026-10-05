"""Mail configuration (#121, decisions.md §4.5): the ``smtp`` settings
validator, ``update_mail_settings`` / ``skip_mail_setup``, the shared form,
``/admin/settings/mail/``, the unconfigured-mail banner, the wizard's Skip,
and the ``use_tls`` -> ``security`` data migration.
"""

from __future__ import annotations

import functools
import importlib
from io import StringIO
from contextlib import redirect_stdout

from django.apps import apps
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse as _reverse
from django.utils import timezone

from audit.models import OutboxEvent
from osds.adapters import override_capability
from osds.tests.mail_stub import email_send_stub
from tenants.mail_forms import MailSettingsForm
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant
from tenants.secrets import get_secret, has_secret, set_secret
from tenants.services import (
    InvalidTenantSettings,
    skip_mail_setup,
    update_mail_settings,
    update_tenant_settings,
)
from tenants.setup_state import next_step

reverse = functools.partial(_reverse, urlconf="osds.urls_tenant")
HOST = "acme.test"
_FAST_HASH = ["django.contrib.auth.hashers.MD5PasswordHasher"]

GOOD = {
    "host": "smtp.example.test",
    "port": 587,
    "security": "starttls",
    "from_email": "noreply@acme.test",
    "username": "bot",
}


def _tenant(**kw):
    kw.setdefault("primary_domain", HOST)
    return Tenant.objects.create(slug="acme", name="Acme", **kw)


class SmtpValidatorTests(TestCase):
    def setUp(self):
        self.tenant = _tenant()
        self.op = Operator.objects.create_user(email="a@acme.test", password="x")

    def put(self, value):
        return update_tenant_settings(
            tenant=self.tenant, changes={"smtp": value}, changed_by=self.op
        )

    def test_empty_block_is_valid(self):
        self.put({})
        self.assertEqual(self.tenant.settings["smtp"], {})

    def test_good_block_is_valid(self):
        self.put(dict(GOOD))
        self.assertEqual(self.tenant.settings["smtp"]["security"], "starttls")

    def test_bad_values_are_rejected_and_nothing_is_stored(self):
        bad = [
            "nope",
            {**GOOD, "security": "ssl"},
            {**GOOD, "security": None},
            {k: v for k, v in GOOD.items() if k != "security"},
            {"host": "h", "use_tls": True},
            {**GOOD, "port": 0},
            {**GOOD, "port": 70000},
            {**GOOD, "port": True},
            {**GOOD, "port": "587"},
            {**GOOD, "host": 5},
            {**GOOD, "security": "none", "username": "bot"},
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(InvalidTenantSettings):
                    self.put(value)
                self.assertNotIn("smtp", Tenant.objects.get().settings)

    def test_username_with_none_is_rejected_but_none_without_username_is_fine(self):
        self.put({**GOOD, "security": "none", "username": ""})


class UpdateMailSettingsTests(TransactionTestCase):
    def setUp(self):
        self.tenant = _tenant()
        self.op = Operator.objects.create_user(email="a@acme.test", password="x")

    def save(self, *, password="", clear_password=False, **overrides):
        return update_mail_settings(
            tenant=self.tenant,
            config={**GOOD, **overrides},
            password=password,
            clear_password=clear_password,
            changed_by=self.op,
        )

    def test_stores_block_and_password_and_emits_settings_changed(self):
        self.save(password="pw1")
        self.assertEqual(Tenant.objects.get().settings["smtp"], GOOD)
        self.assertEqual(get_secret("smtp_password", tenant=self.tenant), "pw1")
        self.assertTrue(
            OutboxEvent.all_tenants.filter(
                type="tenant.settings_changed", tenant=self.tenant
            ).exists()
        )

    def test_password_is_never_in_the_event_or_the_settings(self):
        self.save(password="TOP-SECRET-PW")
        self.assertNotIn("TOP-SECRET-PW", str(Tenant.objects.get().settings))
        for ev in OutboxEvent.all_tenants.filter(tenant=self.tenant):
            self.assertNotIn("TOP-SECRET-PW", str(ev.data))

    def test_blank_password_keeps_the_stored_secret(self):
        self.save(password="pw1")
        self.save(port=2525)
        self.assertEqual(get_secret("smtp_password", tenant=self.tenant), "pw1")
        self.assertEqual(Tenant.objects.get().settings["smtp"]["port"], 2525)

    def test_new_password_replaces_the_secret(self):
        self.save(password="pw1")
        self.save(password="pw2")
        self.assertEqual(get_secret("smtp_password", tenant=self.tenant), "pw2")

    def test_clear_password_removes_the_secret(self):
        self.save(password="pw1")
        self.save(clear_password=True)
        self.assertFalse(has_secret("smtp_password", tenant=self.tenant))

    def test_clearing_the_username_removes_the_secret(self):
        self.save(password="pw1")
        self.save(username="")
        self.assertFalse(has_secret("smtp_password", tenant=self.tenant))

    def test_host_change_with_username_and_blank_password_is_refused(self):
        self.save(password="pw1")
        with self.assertRaises(InvalidTenantSettings) as cm:
            self.save(host="attacker.example.test")
        self.assertIn("re-enter the password", str(cm.exception))
        self.assertEqual(
            Tenant.objects.get().settings["smtp"]["host"], "smtp.example.test"
        )
        self.assertEqual(get_secret("smtp_password", tenant=self.tenant), "pw1")

    def test_host_change_with_a_new_password_is_accepted(self):
        self.save(password="pw1")
        self.save(host="other.example.test", password="pw2")
        self.assertEqual(get_secret("smtp_password", tenant=self.tenant), "pw2")

    def test_host_change_while_removing_the_password_is_accepted(self):
        self.save(password="pw1")
        self.save(host="other.example.test", clear_password=True)
        self.assertFalse(has_secret("smtp_password", tenant=self.tenant))

    def test_host_change_without_a_username_needs_no_password(self):
        self.save(password="pw1")
        self.save(host="other.example.test", username="", security="none")

    def test_host_case_and_whitespace_do_not_count_as_a_change(self):
        self.save(password="pw1")
        self.save(host=" SMTP.Example.Test ")

    def test_orphaned_secret_is_not_sent_to_a_host_set_after_skip(self):
        set_secret("smtp_password", "old", tenant=self.tenant)
        with self.assertRaises(InvalidTenantSettings):
            self.save()

    def test_first_configuration_needs_no_password(self):
        self.save()

    def test_a_rejected_block_leaves_no_new_password_behind(self):
        with self.assertRaises(InvalidTenantSettings):
            self.save(password="pw1", security="none")
        self.assertFalse(has_secret("smtp_password", tenant=self.tenant))

    def test_skip_stores_an_empty_block_and_drops_the_secret(self):
        self.save(password="pw1")
        skip_mail_setup(tenant=self.tenant, changed_by=self.op)
        self.assertEqual(Tenant.objects.get().settings["smtp"], {})
        self.assertFalse(has_secret("smtp_password", tenant=self.tenant))


class MailFormTests(TestCase):
    def test_from_email_prefills_from_primary_domain_verbatim(self):
        form = MailSettingsForm(tenant=_tenant(primary_domain="Dir.Example.test"))
        self.assertEqual(form["from_email"].value(), "noreply@dir.example.test")

    def test_from_email_blank_without_a_domain(self):
        form = MailSettingsForm(tenant=_tenant(primary_domain=None))
        self.assertEqual(form["from_email"].value(), "")

    def test_a_stored_from_email_wins_over_the_prefill(self):
        tenant = _tenant(settings={"smtp": {**GOOD, "from_email": "me@other.test"}})
        self.assertEqual(
            MailSettingsForm(tenant=tenant)["from_email"].value(), "me@other.test"
        )

    def test_defaults(self):
        form = MailSettingsForm(tenant=_tenant())
        self.assertEqual(form["port"].value(), 587)
        self.assertEqual(form["security"].value(), "starttls")

    def test_password_is_never_rendered(self):
        tenant = _tenant(settings={"smtp": dict(GOOD)})
        set_secret("smtp_password", "TOP-SECRET-PW", tenant=tenant)
        html = MailSettingsForm(tenant=tenant).as_p()
        self.assertNotIn("TOP-SECRET-PW", html)
        self.assertIn("A password is stored", html)

    def test_password_is_not_re_rendered_on_a_failed_post(self):
        tenant = _tenant()
        form = MailSettingsForm(
            {**GOOD, "security": "none", "password": "TYPED-PW"}, tenant=tenant
        )
        self.assertFalse(form.is_valid())
        self.assertNotIn("TYPED-PW", form.as_p())

    def test_username_with_none_is_a_form_error(self):
        form = MailSettingsForm({**GOOD, "security": "none"}, tenant=_tenant())
        self.assertFalse(form.is_valid())
        self.assertIn("security", form.errors)

    def test_host_change_with_blank_password_is_a_form_error(self):
        tenant = _tenant(settings={"smtp": dict(GOOD)})
        set_secret("smtp_password", "pw1", tenant=tenant)
        form = MailSettingsForm({**GOOD, "host": "new.example.test"}, tenant=tenant)
        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)

    def test_host_change_with_a_password_is_valid(self):
        tenant = _tenant(settings={"smtp": dict(GOOD)})
        form = MailSettingsForm(
            {**GOOD, "host": "new.example.test", "password": "pw2"}, tenant=tenant
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_password_and_clear_together_is_a_form_error(self):
        form = MailSettingsForm(
            {**GOOD, "password": "x", "clear_password": "on"}, tenant=_tenant()
        )
        self.assertFalse(form.is_valid())
        self.assertIn("clear_password", form.errors)

    def test_security_is_required(self):
        data = {k: v for k, v in GOOD.items() if k != "security"}
        self.assertIn("security", MailSettingsForm(data, tenant=_tenant()).errors)


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class MailSettingsViewTests(TransactionTestCase):
    def setUp(self):
        self.enterContext(email_send_stub(available=False))
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = _tenant()
        self.other = Tenant.objects.create(
            slug="other", name="Other", primary_domain="other.test"
        )
        self.url = reverse("directory_admin:mail-settings")

    def operator(self, role, email="op@acme.test", tenant=None):
        op = Operator.objects.create_user(email=email, password="pw")
        StaffMembership.objects.create(
            operator=op, tenant=tenant or self.tenant, role=role,
            status=StaffMembership.Status.ACTIVE,
        )
        return op

    def client_for(self, op=None):
        c = Client()
        if op:
            c.force_login(op)
        return c

    def get(self, op=None):
        return self.client_for(op).get(self.url, HTTP_HOST=HOST)

    def post(self, op, data):
        return self.client_for(op).post(self.url, data, HTTP_HOST=HOST)

    def test_admin_gets_the_form(self):
        r = self.get(self.operator(StaffMembership.Role.ADMIN))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'name="security"')
        self.assertContains(r, "noreply@acme.test")

    def test_every_lower_role_is_forbidden(self):
        for role in (
            StaffMembership.Role.SUPPORT,
            StaffMembership.Role.MODERATOR,
            StaffMembership.Role.EDITOR,
            StaffMembership.Role.MANAGER,
        ):
            with self.subTest(role=role):
                op = self.operator(role, email=f"r{role}@acme.test")
                self.assertEqual(self.get(op).status_code, 403)
                self.assertEqual(self.post(op, GOOD).status_code, 403)
        self.assertNotIn("smtp", Tenant.objects.get(slug="acme").settings)

    def test_a_non_member_gets_404(self):
        stranger = self.operator(
            StaffMembership.Role.ADMIN, email="s@other.test", tenant=self.other
        )
        self.assertEqual(self.get(stranger).status_code, 404)

    def test_anonymous_is_redirected_to_login(self):
        r = self.get()
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login/", r["Location"])

    def test_a_superadmin_without_a_membership_gets_no_bypass(self):
        root = Operator.objects.create_superuser(email="root@x.test", password="pw")
        self.assertEqual(self.get(root).status_code, 404)

    def test_post_saves_and_redirects(self):
        op = self.operator(StaffMembership.Role.ADMIN)
        r = self.post(op, {**GOOD, "password": "pw1"})
        self.assertEqual(r.status_code, 302)
        tenant = Tenant.objects.get(slug="acme")
        self.assertEqual(tenant.settings["smtp"], GOOD)
        self.assertEqual(get_secret("smtp_password", tenant=tenant), "pw1")

    def test_post_only_touches_the_resolved_tenant(self):
        op = self.operator(StaffMembership.Role.ADMIN)
        self.post(op, {**GOOD, "password": "pw1"})
        other = Tenant.objects.get(slug="other")
        self.assertNotIn("smtp", other.settings)
        self.assertFalse(has_secret("smtp_password", tenant=other))

    def test_invalid_post_rerenders_with_errors_and_no_password(self):
        op = self.operator(StaffMembership.Role.ADMIN)
        r = self.post(op, {**GOOD, "security": "none", "password": "TYPED-PW"})
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "TYPED-PW")
        self.assertNotIn("smtp", Tenant.objects.get(slug="acme").settings)

    def test_stored_password_is_not_in_the_page(self):
        op = self.operator(StaffMembership.Role.ADMIN)
        self.post(op, {**GOOD, "password": "TOP-SECRET-PW"})
        r = self.get(op)
        self.assertNotContains(r, "TOP-SECRET-PW")
        self.assertContains(r, "A password is stored")

    def test_host_change_without_a_password_is_refused_end_to_end(self):
        op = self.operator(StaffMembership.Role.ADMIN)
        self.post(op, {**GOOD, "password": "pw1"})
        r = self.post(op, {**GOOD, "host": "evil.example.test"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "re-enter the password")
        tenant = Tenant.objects.get(slug="acme")
        self.assertEqual(tenant.settings["smtp"]["host"], "smtp.example.test")


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class MailBannerTests(TestCase):
    BANNER = "Email is not configured"

    def setUp(self):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = _tenant()
        self.settings_url = reverse("directory_admin:mail-settings")

    def page(self, role, path="/admin/"):
        op = Operator.objects.create_user(email=f"r{role}@acme.test", password="pw")
        StaffMembership.objects.create(
            operator=op, tenant=self.tenant, role=role,
            status=StaffMembership.Status.ACTIVE,
        )
        c = Client()
        c.force_login(op)
        return c.get(path, HTTP_HOST=HOST)

    def test_every_active_role_sees_it_but_only_admin_gets_the_link(self):
        with email_send_stub(available=False):
            for role in StaffMembership.Role:
                with self.subTest(role=role):
                    r = self.page(role)
                    self.assertContains(r, self.BANNER)
                    if role >= StaffMembership.Role.ADMIN:
                        self.assertContains(r, f'href="{self.settings_url}"')
                    else:
                        self.assertNotContains(r, f'href="{self.settings_url}"')
                        self.assertContains(r, "Ask an administrator")

    def test_hidden_when_mail_is_available(self):
        with email_send_stub(available=True):
            self.assertNotContains(self.page(StaffMembership.Role.ADMIN), self.BANNER)

    def test_shown_with_no_provider_registered(self):
        with override_capability({}):
            self.assertContains(self.page(StaffMembership.Role.ADMIN), self.BANNER)

    def test_shown_on_a_non_index_admin_page(self):
        with email_send_stub(available=False):
            r = self.page(StaffMembership.Role.ADMIN, "/admin/listing-types/")
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, self.BANNER)

    def test_not_shown_on_the_login_page(self):
        with email_send_stub(available=False):
            r = Client().get("/admin/login/", HTTP_HOST=HOST)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, self.BANNER)

    def test_not_shown_to_an_operator_without_a_membership_here(self):
        op = Operator.objects.create_user(email="nobody@acme.test", password="pw")
        c = Client()
        c.force_login(op)
        with email_send_stub(available=False):
            r = c.get("/admin/", HTTP_HOST=HOST)
        self.assertNotContains(r, self.BANNER)

    def test_the_index_links_to_settings_for_admin_only(self):
        with email_send_stub(available=True):
            self.assertContains(
                self.page(StaffMembership.Role.ADMIN), "Mail settings"
            )
            self.assertNotContains(
                self.page(StaffMembership.Role.MANAGER), "Mail settings"
            )


class SmtpSecurityMigrationTests(TestCase):
    def run_migration(self):
        module = importlib.import_module("tenants.migrations.0003_smtp_security")
        out = StringIO()
        with redirect_stdout(out):
            module.use_tls_to_security(apps, None)
        return out.getvalue()

    def smtp(self, slug="acme"):
        return Tenant.objects.get(slug=slug).settings.get("smtp")

    def make(self, slug, smtp):
        return Tenant.objects.create(slug=slug, name=slug, settings={"smtp": smtp})

    def test_true_becomes_starttls_and_false_becomes_none(self):
        self.make("a", {"host": "h", "use_tls": True})
        self.make("b", {"host": "h", "use_tls": False})
        self.run_migration()
        self.assertEqual(self.smtp("a"), {"host": "h", "security": "starttls"})
        self.assertEqual(self.smtp("b"), {"host": "h", "security": "none"})

    def test_a_missing_use_tls_becomes_none_like_the_old_sender(self):
        self.make("a", {"host": "h", "port": 25})
        self.run_migration()
        self.assertEqual(self.smtp("a")["security"], "none")

    def test_an_existing_security_is_kept_and_use_tls_dropped(self):
        self.make("a", {"host": "h", "security": "tls", "use_tls": False})
        self.run_migration()
        self.assertEqual(self.smtp("a"), {"host": "h", "security": "tls"})

    def test_an_empty_block_and_no_block_are_untouched(self):
        self.make("a", {})
        Tenant.objects.create(slug="b", name="b", settings={"storage": {}})
        self.run_migration()
        self.assertEqual(self.smtp("a"), {})
        self.assertIsNone(self.smtp("b"))

    def test_other_settings_keys_survive(self):
        t = self.make("a", {"host": "h", "use_tls": True})
        t.settings["claim_verification"] = {"enabled_methods": ["manual"]}
        t.save(update_fields=["settings"])
        self.run_migration()
        self.assertEqual(
            Tenant.objects.get(slug="a").settings["claim_verification"],
            {"enabled_methods": ["manual"]},
        )

    def test_idempotent(self):
        self.make("a", {"host": "h", "use_tls": True})
        self.run_migration()
        once = self.smtp("a")
        self.run_migration()
        self.assertEqual(self.smtp("a"), once)

    def test_reports_tenants_left_with_a_username_and_no_tls(self):
        self.make("a", {"host": "h", "username": "bot", "use_tls": False})
        self.make("b", {"host": "h", "username": "bot", "use_tls": True})
        self.assertIn("1 tenant(s)", self.run_migration())

    def test_says_nothing_when_no_tenant_is_affected(self):
        self.make("a", {"host": "h", "use_tls": True})
        self.assertEqual(self.run_migration(), "")

    def test_a_migrated_block_validates(self):
        self.make("a", {"host": "h", "from_email": "x@y.test", "use_tls": True})
        self.run_migration()
        op = Operator.objects.create_user(email="a@acme.test", password="x")
        update_tenant_settings(
            tenant=Tenant.objects.get(slug="a"),
            changes={"smtp": self.smtp("a")},
            changed_by=op,
        )


class SkipAdvancesSetupTests(TransactionTestCase):
    def test_an_empty_block_counts_the_step_done(self):
        op = Operator.objects.create_user(email="a@acme.test", password="x")
        tenant = _tenant(settings={"storage": {"backend": "local"}})
        self.assertEqual(next_step(), "smtp")
        skip_mail_setup(tenant=tenant, changed_by=op)
        self.assertEqual(next_step(), "claims")
