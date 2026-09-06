"""Tenant-admin views for listing types + the HTMX schema builder:
access control, valid save, rejection, and the frozen-type rule end to end.
"""

from __future__ import annotations

import functools

from django.test import Client, TestCase, override_settings
from django.urls import reverse as _reverse
from django.utils import timezone

# The tenant admin is mounted on the per-host tenant URLconf, not ROOT_URLCONF.
reverse = functools.partial(_reverse, urlconf="osds.urls_tenant")

from audit.models import OutboxEvent
from directory import services
from directory.models import ListingType, PathRedirect
from osds.tenancy import tenant_context
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant

HOST = "acme.test"


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class SchemaBuilderViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        cls.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain=HOST
        )
        cls.admin = Operator.objects.create_user(email="admin@acme.test", password="x")
        cls.editor = Operator.objects.create_user(email="editor@acme.test", password="x")
        StaffMembership.objects.create(
            operator=cls.admin, tenant=cls.tenant,
            role=StaffMembership.Role.ADMIN, status=StaffMembership.Status.ACTIVE,
        )
        StaffMembership.objects.create(
            operator=cls.editor, tenant=cls.tenant,
            role=StaffMembership.Role.EDITOR, status=StaffMembership.Status.ACTIVE,
        )

    def _client(self, operator=None):
        c = Client()
        if operator:
            c.force_login(operator)
        return c

    def _get(self, client, name, **kw):
        return client.get(reverse(f"directory_admin:{name}", kwargs=kw), HTTP_HOST=HOST)

    def _post(self, client, name, data, **kw):
        return client.post(
            reverse(f"directory_admin:{name}", kwargs=kw), data, HTTP_HOST=HOST
        )

    def _make_type(self, key="business", segment="businesses", fields=None):
        with tenant_context(self.tenant):
            return services.create_listing_type(
                self.tenant, key=key, label_singular=key.title(),
                label_plural=key.title() + "s", path_segment=segment,
                claimable=True, fields=fields or [], actor=self.admin,
            )

    def _settings_events(self):
        return OutboxEvent.all_tenants.filter(
            type="tenant.settings_changed", tenant=self.tenant
        )

    # --- access -----------------------------------------------------------
    def test_anonymous_gets_404(self):
        self.assertEqual(self._get(self._client(), "type-list").status_code, 404)

    def test_editor_role_is_forbidden(self):
        self.assertEqual(
            self._get(self._client(self.editor), "type-list").status_code, 403
        )

    def test_admin_can_list(self):
        self.assertEqual(
            self._get(self._client(self.admin), "type-list").status_code, 200
        )

    def test_no_membership_is_404(self):
        stranger = Operator.objects.create_user(email="x@y.test", password="x")
        self.assertEqual(
            self._get(self._client(stranger), "type-list").status_code, 404
        )

    # --- create ---------------------------------------------------------
    def test_create_type_redirects_to_the_builder_and_emits(self):
        client = self._client(self.admin)
        resp = self._post(
            client, "type-create",
            {"key": "business", "label_singular": "Business",
             "label_plural": "Businesses", "path_segment": "businesses",
             "claimable": "on"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], reverse(
            "directory_admin:type-fields", kwargs={"key": "business"}
        ))
        self.assertTrue(ListingType.all_tenants.filter(key="business").exists())
        change = self._settings_events().last().data["changes"][0]
        self.assertEqual(change["path"], "/listing_types/business")

    def test_second_type_requires_url_change_confirmation(self):
        self._make_type()
        client = self._client(self.admin)
        base = {"key": "software", "label_singular": "Software",
                "label_plural": "Software", "path_segment": "software"}
        resp = self._post(client, "type-create", base)
        self.assertEqual(resp.status_code, 200)  # form invalid, no redirect
        self.assertFalse(ListingType.all_tenants.filter(key="software").exists())

        resp = self._post(client, "type-create", {**base, "confirm_url_change": "on"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            PathRedirect.all_tenants.filter(tenant=self.tenant, old_prefix="").exists()
        )

    def test_edit_form_has_no_key_field(self):
        self._make_type()
        resp = self._get(self._client(self.admin), "type-edit", key="business")
        self.assertNotContains(resp, 'name="key"')

    # --- schema builder ---------------------------------------------------
    def test_builder_saves_valid_fields(self):
        self._make_type()
        client = self._client(self.admin)
        resp = self._post(
            client, "type-fields",
            {
                "field_key": ["years", "notes"],
                "field_label": ["Years in business", "Notes"],
                "field_type": ["integer", "text"],
                "field_required": ["0", "0"],
                "field_public": ["1", "1"],
                "field_searchable": ["0", "1"],
                "field_options": ["", ""],
            },
            key="business",
        )
        self.assertEqual(resp.status_code, 302)
        lt = ListingType.all_tenants.get(key="business")
        self.assertEqual([f["key"] for f in lt.fields], ["years", "notes"])
        self.assertEqual(lt.fields[1]["searchable"], True)
        self.assertEqual(lt.fields[0]["searchable"], False)

    def test_builder_rejects_reserved_key_without_writing(self):
        self._make_type()
        before_events = self._settings_events().count()
        resp = self._post(
            self._client(self.admin), "type-fields",
            {
                "field_key": ["status"],
                "field_label": ["Status"],
                "field_type": ["text"],
                "field_required": ["0"],
                "field_public": ["1"],
                "field_searchable": ["0"],
                "field_options": [""],
            },
            key="business",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "reserved field name")
        self.assertEqual(ListingType.all_tenants.get(key="business").fields, [])
        self.assertEqual(self._settings_events().count(), before_events)

    def test_builder_blocks_retyping_an_existing_field(self):
        self._make_type(fields=[{"key": "years", "label": "Years", "type": "integer"}])
        resp = self._post(
            self._client(self.admin), "type-fields",
            {
                "field_key": ["years"],
                "field_label": ["Years"],
                "field_type": ["text"],
                "field_required": ["0"],
                "field_public": ["1"],
                "field_searchable": ["0"],
                "field_options": [""],
            },
            key="business",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "type is frozen")
        self.assertEqual(
            ListingType.all_tenants.get(key="business").fields[0]["type"], "integer"
        )

    def test_builder_loads_vendored_htmx_not_a_cdn(self):
        self._make_type()
        resp = self._get(self._client(self.admin), "type-fields", key="business")
        self.assertContains(resp, "/static/directory/vendor/htmx-2.0.4.min.js")
        self.assertNotContains(resp, "unpkg")

    def test_add_field_row_returns_a_row_fragment(self):
        self._make_type()
        resp = self._get(self._client(self.admin), "type-fields-row", key="business")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="field_key"')
        self.assertContains(resp, 'name="field_type"')

    def test_remove_field_row_returns_empty(self):
        self._make_type()
        resp = self._client(self.admin).get(
            reverse("directory_admin:type-fields-row", kwargs={"key": "business"})
            + "?blank=1",
            HTTP_HOST=HOST,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"")
