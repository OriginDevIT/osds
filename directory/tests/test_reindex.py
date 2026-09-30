"""Search reindex: markers written on schema/category change, and the
rebuild_search_index command that drains them.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.postgres.search import SearchQuery
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from directory import services
from directory.models import Category, Listing, ListingType, SearchReindexJob
from directory.search import drain_reindex_markers
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


class DrainMarkersTests(TestCase):
    """#132: the shared drain the worker tick job and the command both run."""

    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now()
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")
        cls.lt = ListingType.all_tenants.create(
            tenant=cls.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )
        cls.cat = Category.all_tenants.create(
            tenant=cls.tenant, listing_type=cls.lt, slug="heaters", name="Water Heaters"
        )

    def _stale(self, n, *, tenant=None, cat=None):
        tenant, cat = tenant or self.tenant, cat or self.cat
        made = []
        for i in range(n):
            listing = Listing.all_tenants.create(
                tenant=tenant, listing_type=cat.listing_type,
                slug=f"l{tenant.pk}-{cat.pk}-{i}", name=f"Acme {i}",
            )
            with tenant_context(tenant):
                listing.categories.set([cat])
            made.append(listing)  # search_vector left NULL
        return made

    def _marker(self, cat=None):
        cat = cat or self.cat
        return SearchReindexJob.all_tenants.create(
            tenant=cat.tenant, scope="category", scope_ref=cat.public_id
        )

    def _hit(self, listing):
        return Listing.all_tenants.filter(
            pk=listing.pk, search_vector=SearchQuery("heater", config="english")
        ).exists()

    def test_no_markers_is_idle(self):
        result = drain_reindex_markers(now=self.now)
        self.assertEqual((result.listings, result.markers_done, result.more), (0, 0, False))

    def test_a_small_marker_is_drained_in_one_call(self):
        listings = self._stale(3)
        marker = self._marker()

        result = drain_reindex_markers(now=self.now)

        self.assertEqual((result.listings, result.markers_done, result.more), (3, 1, False))
        marker.refresh_from_db()
        self.assertEqual(marker.done_at, self.now)
        self.assertTrue(all(self._hit(l) for l in listings))

    def test_a_large_marker_is_chunked_and_resumes_from_its_cursor(self):
        listings = self._stale(5)
        marker = self._marker()

        first = drain_reindex_markers(now=self.now, limit=2)
        marker.refresh_from_db()
        self.assertEqual((first.listings, first.more), (2, True))
        self.assertIsNone(marker.done_at)
        self.assertEqual(marker.cursor, listings[1].pk)
        self.assertEqual(sum(self._hit(l) for l in listings), 2)

        second = drain_reindex_markers(now=self.now, limit=2)
        third = drain_reindex_markers(now=self.now, limit=2)

        marker.refresh_from_db()
        self.assertEqual((second.listings, third.listings), (2, 1))
        self.assertFalse(third.more)
        self.assertEqual(marker.done_at, self.now)
        self.assertTrue(all(self._hit(l) for l in listings))

    def test_a_marker_written_during_the_drain_stays_pending(self):
        # The command this replaced stamped every pending marker after a
        # snapshot, losing one written in between.
        self._stale(2)
        first = self._marker()
        late = {}
        real = drain_reindex_markers

        def write_late_marker(listing):
            late.setdefault("m", self._marker())
            from directory.search import recompute_search_vector_v1
            recompute_search_vector_v1(listing)

        with mock.patch("directory.search.recompute_search_vector", write_late_marker):
            real(now=self.now)

        first.refresh_from_db()
        self.assertEqual(first.done_at, self.now)
        late["m"].refresh_from_db()
        self.assertIsNone(late["m"].done_at)

    def test_more_is_true_while_another_marker_waits(self):
        self._stale(1)
        self._marker()
        self._marker()
        self.assertTrue(drain_reindex_markers(now=self.now).more)

    def test_a_marker_only_touches_its_own_tenants_listings(self):
        other = Tenant.objects.create(slug="other", name="Other")
        other_lt = ListingType.all_tenants.create(
            tenant=other, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )
        other_cat = Category.all_tenants.create(
            tenant=other, listing_type=other_lt, slug="heaters", name="Water Heaters"
        )
        mine, theirs = self._stale(1), self._stale(1, tenant=other, cat=other_cat)
        self._marker()

        drain_reindex_markers(now=self.now)

        self.assertTrue(self._hit(mine[0]))
        self.assertFalse(self._hit(theirs[0]))

    def test_a_second_run_is_idle(self):
        self._stale(2)
        self._marker()
        drain_reindex_markers(now=self.now)
        again = drain_reindex_markers(now=self.now)
        self.assertEqual((again.listings, again.more), (0, False))

    def test_the_tick_job_reports_work_and_backlog(self):
        from directory.jobs import search_reindex

        self._stale(3)
        self._marker()
        self._marker()  # a second marker keeps `more` true after the first drains
        first = search_reindex(now=self.now)
        self.assertEqual(first.done, 3 + 1)  # 3 listings + 1 marker finished
        self.assertTrue(first.more)
        second = search_reindex(now=self.now)
        self.assertFalse(second.more)
