"""Reserved slugs (ruling 20): search, admin, media, robots.txt, sitemap.xml,
sitemaps, .well-known are rejected by create_category and upsert_listing.
"""

from __future__ import annotations

from django.test import TestCase, TransactionTestCase

from directory import services
from directory.field_schema import SchemaError
from directory.models import Category, ListingType
from osds.tenancy import tenant_context
from tenants.models import Operator, Tenant

RESERVED = [
    "search", "admin", "media", "robots.txt", "sitemap.xml", "sitemaps",
    ".well-known",
]


class ReservedCategorySlugTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")
        cls.op = Operator.objects.create_user(email="a@a.test", password="x")
        cls.lt = ListingType.all_tenants.create(
            tenant=cls.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )

    def test_create_category_rejects_each_reserved_slug(self):
        for slug in RESERVED:
            with self.subTest(slug=slug), tenant_context(self.tenant):
                with self.assertRaises(SchemaError):
                    services.create_category(
                        self.lt, name="X", slug=slug, parent=None, order=0,
                        actor=self.op,
                    )
        self.assertEqual(Category.all_tenants.count(), 0)

    def test_update_category_rejects_a_reserved_slug(self):
        with tenant_context(self.tenant):
            cat = services.create_category(
                self.lt, name="Plumbers", slug="plumbers", parent=None, order=0,
                actor=self.op,
            )
            with self.assertRaises(SchemaError):
                services.update_category(cat, slug="admin", actor=self.op)


class ReservedListingSlugTests(TransactionTestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )

    def test_upsert_listing_rejects_a_reserved_slug(self):
        for slug in RESERVED:
            with self.subTest(slug=slug), tenant_context(self.tenant):
                with self.assertRaises(SchemaError):
                    services.upsert_listing(
                        self.tenant, listing_type=self.lt,
                        payload={"slug": slug, "name": "X"},
                        actor={"type": "admin", "id": "op_x"}, source="manual",
                    )
