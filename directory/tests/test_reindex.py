"""Search reindex: markers written on schema/category change, and the
rebuild_search_index command that drains them.
"""

from __future__ import annotations

from django.contrib.postgres.search import SearchQuery
from django.core.management import call_command
from django.test import TestCase

from directory import services
from directory.models import Category, Listing, ListingType, SearchReindexJob
from osds.tenancy import tenant_context
from tenants.models import Operator, Tenant

ACTOR_OP = None


class MarkerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")
        cls.op = Operator.objects.create_user(email="a@a.test", password="x")
        cls.lt = ListingType.all_tenants.create(
            tenant=cls.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
            fields=[{"key": "trade", "label": "Trade", "type": "text"}],
        )
        cls.cat = Category.all_tenants.create(
            tenant=cls.tenant, listing_type=cls.lt, slug="plumbers", name="Plumbers"
        )

    def _jobs(self):
        return SearchReindexJob.all_tenants.filter(tenant=self.tenant)

    def test_field_schema_change_writes_a_marker(self):
        with tenant_context(self.tenant):
            services.update_listing_type(
                self.lt,
                fields=[{"key": "trade", "label": "Trade", "type": "text", "searchable": True}],
                actor=self.op,
            )
        job = self._jobs().get()
        self.assertEqual(job.scope, "listing_type")
        self.assertEqual(job.scope_ref, self.lt.public_id)
        self.assertIsNone(job.done_at)

    def test_non_field_change_writes_no_marker(self):
        with tenant_context(self.tenant):
            services.update_listing_type(
                self.lt, label_plural="Companies", actor=self.op
            )
        self.assertFalse(self._jobs().exists())

    def test_category_rename_writes_a_marker(self):
        with tenant_context(self.tenant):
            services.update_category(self.cat, name="Plumbing", actor=self.op)
        job = self._jobs().get()
        self.assertEqual(job.scope, "category")
        self.assertEqual(job.scope_ref, self.cat.public_id)

    def test_category_reorder_writes_no_marker(self):
        with tenant_context(self.tenant):
            services.update_category(self.cat, order=3, actor=self.op)
        self.assertFalse(self._jobs().exists())


class RebuildCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")
        cls.lt = ListingType.all_tenants.create(
            tenant=cls.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )
        cls.cat = Category.all_tenants.create(
            tenant=cls.tenant, listing_type=cls.lt, slug="heaters", name="Old Name"
        )

    def _stale_listing(self):
        listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="x", name="Acme",
        )
        with tenant_context(self.tenant):
            listing.categories.set([self.cat])
        # deliberately leave search_vector NULL
        return listing

    def _hits(self, listing, term):
        return Listing.all_tenants.filter(
            pk=listing.pk, search_vector=SearchQuery(term, config="english")
        ).exists()

    def test_no_markers_is_a_noop(self):
        call_command("rebuild_search_index")  # nothing raised, nothing done

    def test_drains_a_category_marker_and_recomputes(self):
        listing = self._stale_listing()
        self.cat.name = "Water Heaters"
        self.cat.save(update_fields=["name"])
        SearchReindexJob.all_tenants.create(
            tenant=self.tenant, scope="category", scope_ref=self.cat.public_id,
        )
        self.assertFalse(self._hits(listing, "heater"))

        call_command("rebuild_search_index")

        self.assertTrue(self._hits(listing, "heater"))
        self.assertIsNotNone(
            SearchReindexJob.all_tenants.get(scope_ref=self.cat.public_id).done_at
        )

    def test_all_recomputes_every_listing_ignoring_markers(self):
        listing = self._stale_listing()
        call_command("rebuild_search_index", "--all")
        self.assertTrue(self._hits(listing, "acme"))
