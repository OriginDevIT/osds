"""directory.patch -- project() shape and diff() JSON Patch output."""

from __future__ import annotations

from django.test import SimpleTestCase, TestCase

from directory.models import Category, Listing, ListingType
from directory.patch import diff, project
from osds.tenancy import tenant_context
from tenants.models import Tenant


class DiffTests(SimpleTestCase):
    def test_no_change_is_empty(self):
        a = {"name": "X", "location": {"locality": "Chicago"}}
        self.assertEqual(diff(a, dict(a)), [])

    def test_scalar_replace_add_remove(self):
        before = {"name": "Old", "description": None, "slug": "s"}
        after = {"name": "New", "description": "hi", "slug": None}
        self.assertEqual(
            diff(before, after),
            [
                {"op": "add", "path": "/description", "value": "hi"},
                {"op": "replace", "path": "/name", "value": "New"},
                {"op": "remove", "path": "/slug"},
            ],
        )

    def test_nested_recurse_into_location_and_contact(self):
        before = {"location": {"locality": "A", "lat": 1.0}, "contact": {"email": None}}
        after = {"location": {"locality": "B", "lat": 1.0}, "contact": {"email": "x@y.z"}}
        self.assertEqual(
            diff(before, after),
            [
                {"op": "add", "path": "/contact/email", "value": "x@y.z"},
                {"op": "replace", "path": "/location/locality", "value": "B"},
            ],
        )

    def test_categories_and_social_are_whole_array_replace(self):
        before = {"categories": ["a", "b"], "contact": {"social": []}}
        after = {"categories": ["a", "c"], "contact": {"social": [{"platform": "x"}]}}
        self.assertEqual(
            diff(before, after),
            [
                {"op": "replace", "path": "/categories", "value": ["a", "c"]},
                {
                    "op": "replace",
                    "path": "/contact/social",
                    "value": [{"platform": "x"}],
                },
            ],
        )

    def test_external_profiles_is_whole_object_replace(self):
        before = {"external_profiles": {"google": {"place_id": "old"}}}
        after = {"external_profiles": {"google": {"place_id": "new"}}}
        self.assertEqual(
            diff(before, after),
            [
                {
                    "op": "replace",
                    "path": "/external_profiles",
                    "value": {"google": {"place_id": "new"}},
                }
            ],
        )

    def test_custom_fields_diff_per_key(self):
        before = {"custom_fields": {"years": 5, "licence": "AB"}}
        after = {"custom_fields": {"years": 6, "note": "x"}}
        # ops are emitted in sorted-key order
        self.assertEqual(
            diff(before, after),
            [
                {"op": "remove", "path": "/custom_fields/licence"},
                {"op": "add", "path": "/custom_fields/note", "value": "x"},
                {"op": "replace", "path": "/custom_fields/years", "value": 6},
            ],
        )

    def test_none_and_missing_are_equivalent(self):
        self.assertEqual(diff({"a": None}, {}), [])
        self.assertEqual(diff({}, {"a": None}), [])


class ProjectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")
        cls.lt = ListingType.all_tenants.create(
            tenant=cls.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )
        cls.cat_b = Category.all_tenants.create(
            tenant=cls.tenant, listing_type=cls.lt, slug="b", name="B"
        )
        cls.cat_a = Category.all_tenants.create(
            tenant=cls.tenant, listing_type=cls.lt, slug="a", name="A"
        )

    def test_projection_shape_and_sorted_categories(self):
        listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="acme-co", name="Acme Co",
            description="", locality="Chicago", custom_fields={"years": 5},
        )
        with tenant_context(self.tenant):
            listing.categories.set([self.cat_b, self.cat_a])
            shape = project(listing)
        self.assertEqual(shape["categories"], ["a", "b"])
        self.assertIsNone(shape["description"])  # "" -> None
        self.assertEqual(shape["location"]["locality"], "Chicago")
        self.assertIsNone(shape["location"]["lat"])
        self.assertEqual(shape["custom_fields"], {"years": 5})
        self.assertEqual(shape["media"], {"logo": None, "cover": None, "gallery": []})
        for absent in ("id", "status", "visibility", "tier", "owner", "updated_at"):
            self.assertNotIn(absent, shape)
