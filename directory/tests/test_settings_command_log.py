"""Operator settings saves in the command log (#225, decisions.md §4): the lead
form, adapter, listing-type, field-schema and category pages. One
``settings.update`` row per successful save; the payload names the page and the
changed field names and never a value."""

from __future__ import annotations

from django.test import Client, override_settings

from audit.tests.settings_log import assert_one_save, settings_rows
from directory.models import Category
from directory.tests.lead_base import HOST, LeadBase, Role

URL = "https://Hooks.Example.test/in/T0K3N-abc123?x=1"


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class _Base(LeadBase):
    def setUp(self):
        super().setUp()
        self.admin = self.op(Role.ADMIN)
        self.client = Client()
        self.client.force_login(self.admin)

    def post(self, path, data=None):
        return self.client.post(path, data or {}, HTTP_HOST=HOST)

    def one(self, *, since=0, **kw):
        return assert_one_save(self, self.tenant, self.admin, since=since, **kw)


class LeadSettingsTests(_Base):
    def test_save(self):
        self.assertEqual(self.post("/admin/settings/leads/", {}).status_code, 302)  # switch off
        self.one(page="leads", fields=["enabled"])


class AdapterSettingsTests(_Base):
    def test_save_names_secrets_and_never_their_values(self):
        r = self.post("/admin/settings/adapters/webhook/", {
            "action": "save", "enabled": "on", "url": URL, "events": "claim.*",
            "grant_pii_contact": "on",
        })
        self.assertEqual(r.status_code, 200)  # the generated signing secret is shown once
        shown = r.content.decode()
        row = self.one(
            page="adapter.webhook",
            fields=["enabled", "events", "host", "scope_pii_contact", "scope_pii_message",
                    "signing_secret", "url"],
            forbidden=["T0K3N", "abc123", "hooks.example.test", "claim.*"],
        )
        self.assertNotIn("whsec_", str(row.payload))
        self.assertIn("whsec_", shown)

    def test_rotating_a_secret_is_still_its_own_command(self):
        self.post("/admin/settings/adapters/webhook/", {"action": "save", "enabled": "on", "url": URL})
        self.post("/admin/settings/adapters/webhook/", {"action": "rotate", "key": "signing_secret"})
        self.assertEqual(len(settings_rows(self.tenant)), 1)  # the rotation is adapter.rotate_secret

    def test_a_refused_save_is_logged_rejected_and_names_nothing(self):
        r = self.post("/admin/settings/adapters/webhook/", {
            "action": "save", "enabled": "on", "url": URL, "events": "tenant.nope-SECRET",
        })
        self.assertEqual(r.status_code, 200)
        [row] = settings_rows(self.tenant)
        self.assertEqual((row.outcome, row.problem), ("rejected", {"reason": "invalid"}))
        self.assertNotIn("nope-SECRET", str([row.payload, row.problem]))


class ListingTypeTests(_Base):
    def test_create_edit_fields_delete(self):
        r = self.post("/admin/listing-types/new/", {
            "key": "shop", "label_singular": "Shop-LABEL", "label_plural": "Shops", "path_segment": "shops",
            "claimable": "on", "confirm_url_change": "on",
        })
        self.assertEqual(r.status_code, 302)
        self.one(
            page="listing_types", op="add", forbidden=["Shop-LABEL", "shops"],
            fields=["key", "label_singular", "label_plural", "path_segment", "claimable", "fields"],
        )

        r = self.post("/admin/listing-types/shop/", {
            "label_singular": "Shop-LABEL", "label_plural": "Stores-LABEL", "path_segment": "shops",
            "claimable": "on",
        })
        self.assertEqual(r.status_code, 302)
        rows = settings_rows(self.tenant)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].payload, {"page": "listing_types", "op": "replace", "fields": ["label_plural"]})
        self.assertNotIn("Stores-LABEL", str(rows[1].payload))

        r = self.post("/admin/listing-types/shop/fields/", {
            "field_key": ["licence"], "field_label": ["Licence"], "field_type": ["text"],
        })
        self.assertEqual(r.status_code, 302)
        rows = settings_rows(self.tenant)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2].payload, {"page": "listing_types", "op": "replace", "fields": ["fields"]})

        self.assertEqual(self.post("/admin/listing-types/shop/delete/").status_code, 302)
        rows = settings_rows(self.tenant)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[3].payload, {"page": "listing_types", "op": "remove", "fields": []})


class CategoryTests(_Base):
    def test_create_edit_delete(self):
        r = self.post("/admin/listing-types/business/categories/new/",
                      {"name": "Roofers-NAME", "slug": "roofers", "order": "1"})
        self.assertEqual(r.status_code, 302)
        self.one(
            page="categories", op="add", fields=["name", "slug", "parent", "order"],
            forbidden=["Roofers-NAME", "roofers"],
        )
        roofers = Category.all_tenants.get(slug="roofers")

        r = self.post(f"/admin/categories/{roofers.public_id}/",
                      {"name": "Roofers-NAME", "slug": "roofers", "order": "2"})
        self.assertEqual(r.status_code, 302)
        rows = settings_rows(self.tenant)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].payload, {"page": "categories", "op": "replace", "fields": ["order"]})

        self.assertEqual(self.post(f"/admin/categories/{roofers.public_id}/delete/").status_code, 302)
        rows = settings_rows(self.tenant)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2].payload, {"page": "categories", "op": "remove", "fields": []})
