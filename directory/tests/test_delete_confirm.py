"""Confirm-then-delete flow for listing types and categories (issue #129).

GET renders a confirm page naming every destructive effect and removes
nothing; only a POST deletes.
"""

from __future__ import annotations

from django.test import TestCase, TransactionTestCase, override_settings

from tenants.models import StaffMembership
from directory.models import Category, ListingType
from directory.tests.test_listing_admin_views import (
    HOST,
    _FAST_HASH,
    _Setup,
)

_OVERRIDE = dict(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)


class _AdminSetup(_Setup):
    def setUp(self):
        super().setUp()
        self.admin = self._member("admin@acme.test", StaffMembership.Role.ADMIN)


@override_settings(**_OVERRIDE)
class DeleteButtonTests(_AdminSetup, TestCase):
    def test_type_list_has_delete_link_to_confirm(self):
        resp = self._client(self.admin).get(self._url("type-list"), HTTP_HOST=HOST)
        self.assertContains(resp, self._url("type-delete", key="business"))
        self.assertContains(resp, "Delete")

    def test_category_list_delete_is_a_link_not_a_direct_post(self):
        cat = Category.all_tenants.get(slug="plumbers")
        url = self._url("category-delete", public_id=cat.public_id)
        resp = self._client(self.admin).get(
            self._url("category-list", key="business"), HTTP_HOST=HOST
        )
        self.assertContains(resp, f'href="{url}"')
        self.assertNotContains(resp, f'method="post" action="{url}"')


@override_settings(**_OVERRIDE)
class CategoryDeleteConfirmTests(_AdminSetup, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.parent = Category.all_tenants.get(slug="plumbers")
        self.child = Category.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="gas", name="Gas",
            parent=self.parent,
        )
        self.grandchild = Category.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="boilers", name="Boilers",
            parent=self.child,
        )
        self.url = self._url("category-delete", public_id=self.parent.public_id)

    def test_get_confirms_and_deletes_nothing(self):
        listing = self._make_listing()
        listing.categories.add(self.parent)
        before = Category.all_tenants.count()
        resp = self._client(self.admin).get(self.url, HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "this will delete 2 child categories")
        self.assertContains(resp, "1 listing association")
        self.assertEqual(Category.all_tenants.count(), before)

    def test_singular_wording(self):
        self.grandchild.delete()
        resp = self._client(self.admin).get(self.url, HTTP_HOST=HOST)
        self.assertContains(resp, "this will delete 1 child category")

    def test_no_children_states_zero(self):
        resp = self._client(self.admin).get(
            self._url("category-delete", public_id=self.grandchild.public_id),
            HTTP_HOST=HOST,
        )
        self.assertContains(resp, "this will delete 0 child categories")

    def test_post_deletes_category_and_descendants(self):
        resp = self._client(self.admin).post(self.url, HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Category.all_tenants.count(), 0)

    def test_support_rank_forbidden(self):
        resp = self._client(self.support).get(self.url, HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 403)


@override_settings(**_OVERRIDE)
class TypeDeleteConfirmTests(_AdminSetup, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.empty = ListingType.all_tenants.create(
            tenant=self.tenant, key="venue", label_singular="Venue",
            label_plural="Venues", path_segment="venues",
        )
        Category.all_tenants.create(
            tenant=self.tenant, listing_type=self.empty, slug="halls", name="Halls"
        )
        self.url = self._url("type-delete", key="venue")

    def test_get_confirms_and_deletes_nothing(self):
        resp = self._client(self.admin).get(self.url, HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "this will delete the listing type")
        self.assertContains(resp, "1 category")
        self.assertTrue(ListingType.all_tenants.filter(key="venue").exists())

    def test_post_deletes_type_and_its_categories(self):
        resp = self._client(self.admin).post(self.url, HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ListingType.all_tenants.filter(key="venue").exists())
        self.assertFalse(Category.all_tenants.filter(slug="halls").exists())

    def test_type_with_listings_warns_and_post_is_refused_by_service(self):
        self._make_listing()
        url = self._url("type-delete", key="business")
        resp = self._client(self.admin).get(url, HTTP_HOST=HOST)
        self.assertContains(resp, "1 listing")
        self.assertContains(resp, "refused")
        self._client(self.admin).post(url, HTTP_HOST=HOST)
        self.assertTrue(ListingType.all_tenants.filter(key="business").exists())
