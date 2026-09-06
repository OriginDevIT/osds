"""directory.search.recompute_search_vector -- weights, stemming, the tenant
text-search config, and searchable custom fields.
"""

from __future__ import annotations

import importlib
import inspect

from django.contrib.postgres.search import SearchQuery
from django.test import SimpleTestCase, TestCase

from directory.models import Category, Listing, ListingType
from directory.search import (
    recompute_search_vector,
    recompute_search_vector_v1,
    reindex_queryset,
    search_config_for,
)
from osds.tenancy import tenant_context
from tenants.models import Tenant


class FrozenContractTests(SimpleTestCase):
    def test_v1_takes_exactly_one_listing_arg(self):
        self.assertEqual(
            list(inspect.signature(recompute_search_vector_v1).parameters), ["listing"]
        )

    def test_recompute_delegates_to_v1(self):
        self.assertIn(
            "recompute_search_vector_v1(listing)",
            inspect.getsource(recompute_search_vector),
        )

    def test_migration_0004_uses_the_frozen_name_and_shared_queryset(self):
        mig = importlib.import_module("directory.migrations.0004_search_vector")
        src = inspect.getsource(mig.backfill_search_vectors)
        self.assertIn("recompute_search_vector_v1", src)
        self.assertIn("reindex_queryset()", src)
        self.assertNotIn("Listing.objects.all()", src)



class RecomputeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")
        cls.lt = ListingType.all_tenants.create(
            tenant=cls.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
            fields=[
                {"key": "trade", "label": "Trade", "type": "text", "searchable": True},
                {"key": "notes", "label": "Notes", "type": "text", "searchable": False},
            ],
        )
        cls.cat = Category.all_tenants.create(
            tenant=cls.tenant, listing_type=cls.lt, slug="heaters",
            name="Water Heaters",
        )

    def _make(self, **kw):
        kw.setdefault("slug", "x")
        kw.setdefault("name", "X")
        listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, **kw
        )
        with tenant_context(self.tenant):
            if "cats" in kw:
                pass
            recompute_search_vector(listing)
        return listing

    def _hits(self, listing, term, config=None):
        cfg = config or search_config_for(self.tenant)
        return Listing.all_tenants.filter(
            pk=listing.pk, search_vector=SearchQuery(term, config=cfg)
        ).exists()

    def test_name_is_indexed_and_stemmed(self):
        # issue #31: 'plumber' must match 'Plumbers' with the english config.
        listing = self._make(name="Hoffman Plumbers")
        self.assertTrue(self._hits(listing, "plumber"))
        self.assertTrue(self._hits(listing, "hoffman"))

    def test_description_is_indexed(self):
        listing = self._make(description="emergency drain unblocking service")
        self.assertTrue(self._hits(listing, "unblock"))

    def test_category_name_feeds_the_vector(self):
        listing = self._make(name="Acme")
        with tenant_context(self.tenant):
            listing.categories.set([self.cat])
            recompute_search_vector(listing)
        self.assertTrue(self._hits(listing, "heater"))

    def test_searchable_custom_field_feeds_but_non_searchable_does_not(self):
        listing = self._make(
            name="Acme", custom_fields={"trade": "roofing", "notes": "confidential"}
        )
        self.assertTrue(self._hits(listing, "roofing"))
        self.assertFalse(self._hits(listing, "confidential"))

    def test_locality_and_region_feed_weight_d(self):
        listing = self._make(name="Acme", locality="Lakeview", region="Illinois")
        self.assertTrue(self._hits(listing, "lakeview"))
        self.assertTrue(self._hits(listing, "illinois"))

    def test_simple_config_does_not_stem(self):
        self.tenant.settings = {"search_config": "simple"}
        self.tenant.save(update_fields=["settings"])
        listing = self._make(name="Hoffman Plumbing")
        self.assertTrue(self._hits(listing, "plumbing", config="simple"))
        self.assertFalse(self._hits(listing, "plumber", config="simple"))

    def test_config_default_is_english(self):
        self.assertEqual(search_config_for(self.tenant), "english")

    def test_reindex_queryset_prefetches_what_v1_reads(self):
        with tenant_context(self.tenant):
            qs = reindex_queryset()
        self.assertIn("listing_type", qs.query.select_related)
        self.assertEqual(qs._prefetch_related_lookups, ("categories",))
