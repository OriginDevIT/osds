"""The tenant-admin lead pages and the lead-form switch (decisions.md §4.10):
role gating, masking, the access log, mark-spam, and the ADMIN toggle.
"""

from __future__ import annotations

from django.test import Client, override_settings

from audit.models import AccessLog, CommandLog, OutboxEvent
from directory.models import Lead
from directory.tests.lead_base import HOST, LeadBase, Role
from tenants import services as tenant_services
from tenants.models import Tenant

SECRET_EMAIL = "priya.private@example.test"
SECRET_PHONE = "+13125550188"


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class _AdminBase(LeadBase):
    def setUp(self):
        super().setUp()
        self.lead = self.create(
            contact={"name": "Priya R.", "email": SECRET_EMAIL, "phone_e164": SECRET_PHONE},
            message="Burst pipe, please call.",
        )

    def client_as(self, role, *, tenant=None):
        client = Client()
        client.force_login(self.op(role, tenant=tenant))
        return client

    def get(self, client, path):
        return client.get(path, HTTP_HOST=HOST)

    def post(self, client, path, data=None):
        return client.post(path, data or {}, HTTP_HOST=HOST)


class ListTests(_AdminBase):
    def test_support_is_forbidden_and_anonymous_is_sent_to_login(self):
        self.assertEqual(self.get(self.client_as(Role.SUPPORT), "/admin/leads/").status_code, 403)
        r = self.get(Client(), "/admin/leads/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login/", r.url)

    def test_a_non_member_gets_a_404_not_a_403(self):
        other = Tenant.objects.create(slug="other", name="Other")
        r = self.get(self.client_as(Role.ADMIN, tenant=other), "/admin/leads/")
        self.assertEqual(r.status_code, 404)

    def test_a_moderator_sees_the_inbox_with_the_address_masked(self):
        r = self.get(self.client_as(Role.MODERATOR), "/admin/leads/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.listing.name)
        self.assertNotContains(r, SECRET_EMAIL)
        self.assertContains(r, "unclaimed")

    def test_the_spam_tab_lists_only_spam(self):
        Lead.all_tenants.filter(pk=self.lead.pk).update(marked_spam=True)
        moderator = self.client_as(Role.MODERATOR)
        self.assertContains(self.get(moderator, "/admin/leads/?tab=spam"), "Spam (1)")
        self.assertContains(self.get(moderator, "/admin/leads/"), "Inbox (0)")

    def test_another_tenants_leads_are_not_listed(self):
        other = Tenant.objects.create(slug="other", name="Other", primary_domain="other.test")
        self.assertEqual(Lead.all_tenants.filter(tenant=other).count(), 0)
        self.assertContains(self.get(self.client_as(Role.MODERATOR), "/admin/leads/"), "Inbox (1)")


class DetailTests(_AdminBase):
    def url(self):
        return f"/admin/leads/{self.lead.public_id}/"

    def test_a_moderator_reads_the_message_but_not_the_contact_or_consent(self):
        r = self.get(self.client_as(Role.MODERATOR), self.url())
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Burst pipe, please call.")
        self.assertContains(r, "Typed by a visitor")
        self.assertNotContains(r, SECRET_EMAIL)
        self.assertNotContains(r, SECRET_PHONE)
        self.assertNotContains(r, "lead-consent-v1")
        self.assertContains(r, "masked at your role")

    def test_a_manager_sees_everything_including_the_consent_record(self):
        r = self.get(self.client_as(Role.MANAGER), self.url())
        self.assertContains(r, SECRET_EMAIL)
        self.assertContains(r, SECRET_PHONE)
        self.assertContains(r, "contact_by_business: granted")
        self.assertContains(r, "lead-consent-v1")
        self.assertNotContains(r, "masked at your role")

    def test_the_message_is_escaped(self):
        Lead.all_tenants.filter(pk=self.lead.pk).update(message="<script>alert(1)</script>")
        r = self.get(self.client_as(Role.MODERATOR), self.url())
        self.assertNotContains(r, "<script>alert(1)</script>")
        self.assertContains(r, "&lt;script&gt;")

    def test_every_detail_view_writes_an_access_log_row(self):
        moderator = self.client_as(Role.MODERATOR)
        self.get(moderator, self.url())
        manager = self.client_as(Role.MANAGER)
        self.get(manager, self.url())
        admin = self.client_as(Role.ADMIN)
        self.get(admin, self.url())
        rows = list(AccessLog.all_tenants.order_by("id"))
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(
                (row.action, row.resource_type, row.resource_id, row.tenant_id),
                ("viewed", "lead", self.lead.public_id, self.tenant.pk),
            )
            self.assertEqual(row.ip, "127.0.0.1")
        self.assertEqual([r.extra for r in rows], [{"masked": True}, {"masked": False}, {"masked": False}])
        self.assertEqual([r.actor["type"] for r in rows], ["staff", "staff", "admin"])

    def test_a_refused_view_writes_no_access_log_row(self):
        self.get(self.client_as(Role.SUPPORT), self.url())
        self.get(Client(), self.url())
        self.assertFalse(AccessLog.all_tenants.exists())

    def test_the_list_page_is_not_logged(self):
        self.get(self.client_as(Role.MODERATOR), "/admin/leads/")
        self.assertFalse(AccessLog.all_tenants.exists())

    def test_another_tenants_lead_is_a_404(self):
        other = Tenant.objects.create(slug="other", name="Other")
        r = self.get(self.client_as(Role.ADMIN, tenant=other), self.url())
        self.assertEqual(r.status_code, 404)
        self.assertFalse(AccessLog.all_tenants.exists())


class MarkSpamTests(_AdminBase):
    def url(self):
        return f"/admin/leads/{self.lead.public_id}/spam/"

    def test_a_moderator_marks_a_lead_and_lands_back_on_it(self):
        r = self.post(self.client_as(Role.MODERATOR), self.url())
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Lead.all_tenants.get(pk=self.lead.pk).marked_spam)
        self.assertTrue(OutboxEvent.all_tenants.filter(type="lead.marked_spam").exists())
        self.assertEqual(CommandLog.objects.get(command="lead.mark_spam").outcome, "applied")

    def test_support_cannot_and_nothing_changes(self):
        r = self.post(self.client_as(Role.SUPPORT), self.url())
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Lead.all_tenants.get(pk=self.lead.pk).marked_spam)

    def test_a_get_is_not_allowed(self):
        self.assertEqual(self.get(self.client_as(Role.MODERATOR), self.url()).status_code, 405)

    def test_marking_twice_shows_the_refusal(self):
        moderator = self.client_as(Role.MODERATOR)
        self.post(moderator, self.url())
        r = moderator.post(self.url(), HTTP_HOST=HOST, follow=True)
        self.assertContains(r, "already marked as spam")


class SettingsTests(_AdminBase):
    url = "/admin/settings/leads/"

    def test_only_an_admin_may_open_it(self):
        self.assertEqual(self.get(self.client_as(Role.MANAGER), self.url).status_code, 403)
        self.assertEqual(self.get(self.client_as(Role.ADMIN), self.url).status_code, 200)

    def test_the_page_shows_the_wording_visitors_see(self):
        r = self.get(self.client_as(Role.ADMIN), self.url)
        self.assertContains(r, "may contact you about your inquiry")
        self.assertContains(r, "lead-consent-v1")

    def test_turning_the_switch_on_and_off(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(settings={})
        admin = self.client_as(Role.ADMIN)
        self.assertEqual(self.get(Client(), f"/lead/{self.listing.public_id}/").status_code, 404)

        self.post(admin, self.url, {"enabled": "on"})
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.settings["leads"], {"enabled": True})
        self.assertEqual(self.get(Client(), f"/lead/{self.listing.public_id}/").status_code, 200)
        self.assertTrue(OutboxEvent.all_tenants.filter(type="tenant.settings_changed").exists())

        self.post(admin, self.url, {})
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.settings["leads"], {"enabled": False})
        self.assertEqual(self.get(Client(), f"/lead/{self.listing.public_id}/").status_code, 404)

    def test_the_admin_home_links_by_role(self):
        for role, leads_link, settings_link in (
            (Role.SUPPORT, False, False),
            (Role.MODERATOR, True, False),
            (Role.ADMIN, True, True),
        ):
            with self.subTest(role=role):
                r = self.get(self.client_as(role), "/admin/")
                self.assertEqual("/admin/leads/" in r.content.decode(), leads_link)
                self.assertEqual("/admin/settings/leads/" in r.content.decode(), settings_link)


class ValidatorTests(LeadBase):
    def update(self, value):
        operator = self.op(Role.ADMIN)
        return tenant_services.update_tenant_settings(
            tenant=self.tenant, changes={"leads": value}, changed_by=operator
        )

    def test_accepts_enabled_true_and_false(self):
        self.update({"enabled": True})
        self.update({"enabled": False})
        self.update({})

    def test_rejects_the_wrong_shapes(self):
        for bad in ("yes", ["enabled"], {"enabled": "yes"}, {"enabled": 1}, {"enabled": True, "x": 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(tenant_services.InvalidTenantSettings):
                    self.update(bad)
