"""Migration directory/0004_search_vector -- its RunPython backfill, run
against populated tables.

CI migrates from zero, so the backfill normally sees empty tables and its body
never executes. This test rolls the directory app back to the state before
0004, inserts rows through the historical models, applies 0004, and asserts the
backfill populated ``search_vector`` for every row.
"""

from __future__ import annotations

from django.contrib.postgres.search import SearchQuery
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [("directory", "0003_pathredirect")]
TARGET = [("directory", "0004_search_vector")]


class SearchVectorBackfillMigrationTests(TransactionTestCase):
    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def setUp(self):
        old_apps = self._migrate(BEFORE)
        Tenant = old_apps.get_model("tenants", "Tenant")
        ListingType = old_apps.get_model("directory", "ListingType")
        Category = old_apps.get_model("directory", "Category")
        Listing = old_apps.get_model("directory", "Listing")

        tenant = Tenant.objects.create(slug="acme", name="Acme")
        other = Tenant.objects.create(slug="globex", name="Globex")
        self.tenant_pk, self.other_pk = tenant.pk, other.pk

        def make_type(t):
            return ListingType.objects.create(
                tenant=t, key="business", label_singular="B",
                label_plural="Bs", path_segment="businesses",
                fields=[{"key": "trade", "label": "Trade", "type": "text",
                         "searchable": True}],
            )

        lt, other_lt = make_type(tenant), make_type(other)
        cat = Category.objects.create(
            tenant=tenant, listing_type=lt, slug="heaters", name="Water Heaters"
        )
        first = Listing.objects.create(
            tenant=tenant, listing_type=lt, slug="hoffman", name="Hoffman Plumbers",
            description="Emergency repairs",
        )
        first.categories.add(cat)
        Listing.objects.create(
            tenant=tenant, listing_type=lt, slug="second", name="Second Shop",
        )
        Listing.objects.create(
            tenant=other, listing_type=other_lt, slug="initech", name="Initech Roofing",
        )

        # Pre-migration state has no search_vector column to inspect at all.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'listings' AND column_name = 'search_vector'"
            )
            self.assertIsNone(cursor.fetchone())

    def tearDown(self):
        # Leave the schema fully migrated for the tests that follow.
        self._migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def _vectors(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT slug, search_vector::text FROM listings ORDER BY slug")
            return dict(cursor.fetchall())

    def test_backfill_populates_search_vector_for_existing_rows(self):
        self._migrate(TARGET)
        vectors = self._vectors()

        self.assertEqual(set(vectors), {"hoffman", "second", "initech"})
        for slug, vector in vectors.items():
            self.assertTrue(vector, f"search_vector empty for {slug}")

        # Name (A), category (B) and description (C) all reached the vector,
        # stemmed by the english config.
        self.assertIn("plumber", vectors["hoffman"])
        self.assertIn("water", vectors["hoffman"])
        self.assertIn("emerg", vectors["hoffman"])

    def test_backfill_covers_every_tenant_and_is_queryable(self):
        self._migrate(TARGET)
        from directory.models import Listing

        for term, slug in [("plumber", "hoffman"), ("roofing", "initech"),
                           ("shop", "second")]:
            hits = Listing.all_tenants.filter(
                search_vector=SearchQuery(term, config="english")
            ).values_list("slug", flat=True)
            self.assertEqual(list(hits), [slug])
