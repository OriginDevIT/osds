"""Listing-type create form (#128): a field's error renders with that field,
path_segment defaults from key, and its help text states the format."""

from __future__ import annotations

from django.test import Client, override_settings

from directory.models import ListingType
from directory.tests.lead_base import HOST, LeadBase, Role


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class ListingTypeFormTests(LeadBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.op(Role.ADMIN))

    def post(self, data):
        return self.client.post("/admin/listing-types/new/", data, HTTP_HOST=HOST)

    def base(self, **over):
        data = {
            "key": "shop", "label_singular": "Shop", "label_plural": "Shops",
            "path_segment": "shops", "claimable": "on", "confirm_url_change": "on",
        }
        data.update(over)
        return data

    def test_path_segment_error_renders_after_its_own_input(self):
        r = self.post(self.base(path_segment="/shops"))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        err = html.index("Enter a valid")
        self.assertGreater(err, html.index('name="path_segment"'))
        self.assertLess(err, html.index('name="claimable"'))

    def test_label_plural_error_does_not_land_on_path_segment(self):
        html = self.post(self.base(label_plural="")).content.decode()
        err = html.index("This field is required")
        self.assertGreater(err, html.index('name="label_plural"'))
        self.assertLess(err, html.index('name="path_segment"'))

    def test_blank_path_segment_defaults_to_key(self):
        r = self.post(self.base(path_segment=""))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(ListingType.all_tenants.get(tenant=self.tenant, key="shop").path_segment, "shop")

    def test_form_carries_prefill_hook_and_format_help(self):
        html = self.client.get("/admin/listing-types/new/", HTTP_HOST=HOST).content.decode()
        self.assertIn("listing-type-form.js", html)
        self.assertIn("leading slash", html)
