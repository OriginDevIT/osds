"""directory.search.search -- published-only, trigram fallback, blended
ordering, and radius composition.
"""

from __future__ import annotations

import pathlib

from django.test import SimpleTestCase, TestCase

from billing.models import Tier
from directory.models import Listing, ListingType
from directory.search import search
from osds.tenancy import tenant_context
from tenants.models import Tenant


class PublishedHelperGuardTests(SimpleTestCase):
    def test_search_routes_through_the_published_helper(self):
        src = (pathlib.Path(__file__).parents[1] / "search.py").read_text("utf-8")
        self.assertIn("Listing.objects.published()", src)
        # the visibility filter is not re-implemented inline
        self.assertNotIn("visibility=", src)


class SearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")
        cls.lt = ListingType.all_tenants.create(
            tenant=cls.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )

    def _listing(self, name, *, slug=None, visibility="published", **kw):
        from directory.search import recompute_search_vector

        listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt,
            slug=slug or name.lower().replace(" ", "-"),
            name=name, visibility=visibility, **kw,
        )
        with tenant_context(self.tenant):
            recompute_search_vector(listing)
        return listing

    def _search(self, **kw):
        with tenant_context(self.tenant):
            return list(search(self.tenant, self.lt, **kw))

    def test_only_published_listings_are_returned(self):
        self._listing("Hoffman Plumbing", visibility="published")
        self._listing("Draft Plumbing", visibility="draft")
        self._listing("Hidden Plumbing", visibility="hidden")
        names = [o.name for o in self._search(q="plumbing")]
        self.assertEqual(names, ["Hoffman Plumbing"])

    def test_finds_by_stemmed_term(self):
        self._listing("Water Heater Repair")
        self.assertTrue(self._search(q="heaters"))

    def test_trigram_fallback_on_a_typo(self):
        self._listing("Hoffman Plumbing")
        hits = self._search(q="Hofman")  # misspelled, no FTS match
        self.assertEqual([o.name for o in hits], ["Hoffman Plumbing"])

    def _featured(self, rank=3):
        return Tier.all_tenants.create(
            tenant=self.tenant, key="featured", name="Featured", rank=rank
        )

    def test_a_better_match_on_a_free_listing_beats_a_featured_weak_match(self):
        lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="svc", label_singular="S", label_plural="Ss",
            path_segment="svc",
            fields=[{"key": "trade", "label": "Trade", "type": "text", "searchable": True}],
        )
        featured = self._featured()
        weak = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug="roofers", name="City Roofers",
            visibility="published", custom_fields={"trade": "plumbing"},
        )
        Listing.all_tenants.filter(pk=weak.pk).update(current_tier=featured)
        strong = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug="hp", name="Hoffman Plumbing",
            visibility="published",
        )
        with tenant_context(self.tenant):
            from directory.search import recompute_search_vector

            for o in (weak, strong):
                recompute_search_vector(o)
            results = list(search(self.tenant, lt, q="plumbing"))
        self.assertEqual(results[0].pk, strong.pk)
        self.assertIn(weak.pk, [o.pk for o in results])

    def test_tier_breaks_a_relevance_tie(self):
        featured = self._featured()
        free = self._listing("Acme Plumbing", slug="acme-free")
        boosted = self._listing("Acme Plumbing", slug="acme-featured")
        Listing.all_tenants.filter(pk=boosted.pk).update(current_tier=featured)
        results = self._search(q="acme plumbing")
        self.assertEqual([o.pk for o in results], [boosted.pk, free.pk])

    def test_radius_only_orders_by_distance(self):
        near = self._listing("Near Co", lat="41.90", lon="-87.60", geo_precision="rooftop")
        far = self._listing("Far Co", lat="41.98", lon="-87.60", geo_precision="rooftop")
        out = self._search(near=(41.90, -87.60, 50))
        self.assertEqual([o.pk for o in out], [near.pk, far.pk])

    def test_radius_excludes_listings_outside_the_circle(self):
        self._listing("In", lat="41.90", lon="-87.60", geo_precision="rooftop")
        self._listing("Out", lat="42.90", lon="-87.60", geo_precision="rooftop")
        out = self._search(near=(41.90, -87.60, 25))
        self.assertEqual([o.name for o in out], ["In"])

    def test_query_and_radius_compose(self):
        self._listing("Loop Plumbing", lat="41.88", lon="-87.63", geo_precision="rooftop")
        self._listing("Suburb Plumbing", lat="42.20", lon="-88.00", geo_precision="rooftop")
        out = self._search(q="plumbing", near=(41.88, -87.63, 20))
        self.assertEqual([o.name for o in out], ["Loop Plumbing"])
