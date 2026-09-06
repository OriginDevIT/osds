"""The public directory site: catch-all routing (single vs multi type),
PathRedirect 301s, published-only, canonical, and indexability.
"""

from __future__ import annotations

import pathlib

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from directory.models import Category, Listing, ListingType, PathRedirect
from directory.search import recompute_search_vector
from osds.tenancy import tenant_context
from tenants.models import InstallSetup, Tenant

HOST = "acme.test"


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class _Base(TestCase):
    def setUp(self):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme Directory", primary_domain=HOST
        )
        self.client = Client()

    def get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=HOST, **kw)

    def _type(self, key="business", segment="businesses"):
        return ListingType.all_tenants.create(
            tenant=self.tenant, key=key, label_singular=key.title(),
            label_plural=key.title() + "s", path_segment=segment,
        )

    def _cat(self, lt, slug, name=None, order=0, parent=None):
        return Category.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug=slug,
            name=name or slug.title(), order=order, parent=parent,
        )

    def _listing(self, lt, slug, *, name=None, visibility="published", cats=(), **kw):
        listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug=slug,
            name=name or slug.title(), visibility=visibility, **kw,
        )
        with tenant_context(self.tenant):
            if cats:
                listing.categories.set(cats)
            recompute_search_vector(listing)
        return listing


class SingleTypeRoutingTests(_Base):
    def setUp(self):
        super().setUp()
        self.lt = self._type()
        self.plumbers = self._cat(self.lt, "plumbers", order=0)
        self.roofers = self._cat(self.lt, "roofers", order=1)
        self.acme = self._listing(
            self.lt, "acme-co", name="Acme Co", cats=[self.plumbers]
        )

    def test_home_lists_categories_no_segment(self):
        r = self.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'href="/plumbers/"')
        self.assertNotContains(r, "/businesses/")

    def test_category_page(self):
        r = self.get("/plumbers/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Acme Co")
        self.assertContains(r, 'href="/plumbers/acme-co"')

    def test_category_page_without_trailing_slash(self):
        self.assertEqual(self.get("/plumbers").status_code, 200)

    def test_listing_detail(self):
        r = self.get("/plumbers/acme-co")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Acme Co")
        self.assertContains(r, '<link rel="canonical" href="/plumbers/acme-co">')

    def test_unknown_category_is_404(self):
        r = self.get("/ghosts/")
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, "Not found", status_code=404)

    def test_unknown_listing_is_404(self):
        self.assertEqual(self.get("/plumbers/nobody").status_code, 404)

    def test_listing_not_in_that_category_is_404(self):
        self.assertEqual(self.get("/roofers/acme-co").status_code, 404)

    def test_too_many_segments_is_404(self):
        self.assertEqual(self.get("/plumbers/acme-co/extra").status_code, 404)


class PublishedOnlyTests(_Base):
    def setUp(self):
        super().setUp()
        self.lt = self._type()
        self.cat = self._cat(self.lt, "plumbers")
        self.pub = self._listing(self.lt, "pub", name="Published", cats=[self.cat])
        self.draft = self._listing(
            self.lt, "draft", name="Draft", visibility="draft", cats=[self.cat]
        )
        self.hidden = self._listing(
            self.lt, "hidden", name="Hidden", visibility="hidden", cats=[self.cat]
        )

    def test_draft_and_hidden_absent_from_category_list_and_count(self):
        r = self.get("/plumbers/")
        self.assertContains(r, "Published")
        self.assertNotContains(r, ">Draft<")
        self.assertNotContains(r, ">Hidden<")
        self.assertContains(r, "1 listing")

    def test_draft_detail_is_404(self):
        self.assertEqual(self.get("/plumbers/draft").status_code, 404)

    def test_hidden_detail_is_404(self):
        self.assertEqual(self.get("/plumbers/hidden").status_code, 404)

    def test_search_returns_published_only(self):
        r = self.get("/search/?q=draft")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, ">Draft<")
        self.assertContains(r, 'name="robots" content="noindex')


class CanonicalTests(_Base):
    def test_detail_via_non_canonical_category_points_to_lowest_order_slug(self):
        lt = self._type()
        low = self._cat(lt, "aaa", order=0)
        high = self._cat(lt, "zzz", order=5)
        listing = self._listing(lt, "multi", name="Multi", cats=[low, high])
        r = self.get("/zzz/multi")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '<link rel="canonical" href="/aaa/multi">')


class IndexabilityTests(_Base):
    def setUp(self):
        super().setUp()
        self.lt = self._type()
        self.big = self._cat(self.lt, "big")
        self.small = self._cat(self.lt, "small")
        for i in range(5):
            self._listing(self.lt, f"b{i}", name=f"B{i}", cats=[self.big])
        self._listing(self.lt, "s0", name="S0", cats=[self.small])

    def test_page_one_of_a_full_category_is_indexable(self):
        r = self.get("/big/")
        self.assertNotContains(r, 'content="noindex')

    @override_settings(OSDS_PUBLIC_PAGE_SIZE=2)
    def test_page_two_is_noindex(self):
        r = self.get("/big/?page=2")  # 5 listings, 2 per page
        self.assertContains(r, 'content="noindex,follow')

    def test_category_below_three_listings_is_noindex(self):
        r = self.get("/small/")
        self.assertContains(r, 'content="noindex,follow')


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class MultiTypeRoutingTests(_Base):
    def setUp(self):
        super().setUp()
        self.biz = self._type("business", "businesses")
        self.soft = self._type("software", "software")
        self.plumbers = self._cat(self.biz, "plumbers")
        self.acme = self._listing(
            self.biz, "acme-co", name="Acme Co", cats=[self.plumbers]
        )

    def test_home_is_a_type_picker(self):
        r = self.get("/")
        self.assertContains(r, 'href="/businesses/"')
        self.assertContains(r, 'href="/software/"')

    def test_type_landing(self):
        r = self.get("/businesses/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'href="/businesses/plumbers/"')

    def test_prefixed_category_and_detail(self):
        self.assertEqual(self.get("/businesses/plumbers/").status_code, 200)
        r = self.get("/businesses/plumbers/acme-co")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '<link rel="canonical" href="/businesses/plumbers/acme-co">')

    def test_unprefixed_path_is_404(self):
        self.assertEqual(self.get("/plumbers/").status_code, 404)

    def test_unknown_segment_is_404(self):
        self.assertEqual(self.get("/widgets/plumbers/").status_code, 404)


class RedirectTests(_Base):
    def setUp(self):
        super().setUp()
        self.lt = self._type("business", "businesses")

    def test_prefix_redirect_is_a_301(self):
        PathRedirect.all_tenants.create(
            tenant=self.tenant, old_prefix="/businesses", new_prefix="/companies"
        )
        r = self.get("/businesses/plumbers/acme")
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r["Location"], "/companies/plumbers/acme")

    def test_empty_prefix_prepends_new_prefix(self):
        PathRedirect.all_tenants.create(
            tenant=self.tenant, old_prefix="", new_prefix="/businesses"
        )
        r = self.get("/plumbers/acme")
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r["Location"], "/businesses/plumbers/acme")

    def test_longest_prefix_wins(self):
        PathRedirect.all_tenants.create(
            tenant=self.tenant, old_prefix="", new_prefix="/x"
        )
        PathRedirect.all_tenants.create(
            tenant=self.tenant, old_prefix="/businesses/plumbers",
            new_prefix="/businesses/plumbing",
        )
        r = self.get("/businesses/plumbers/acme")
        self.assertEqual(r["Location"], "/businesses/plumbing/acme")

    def test_redirect_not_applied_to_reserved_top_paths(self):
        PathRedirect.all_tenants.create(
            tenant=self.tenant, old_prefix="", new_prefix="/businesses"
        )
        # robots.txt has no route yet (PR 4) -> 404, not a 301
        self.assertEqual(self.get("/robots.txt").status_code, 404)


class PublishedGuardTests(SimpleTestCase):
    def test_public_views_only_query_listings_through_published(self):
        import re

        src = (
            pathlib.Path(__file__).parents[1] / "public_views.py"
        ).read_text("utf-8")
        calls = re.findall(r"Listing\.objects\.(\w+)", src)
        self.assertTrue(calls, "expected at least one Listing.objects.published() call")
        self.assertEqual(
            set(calls), {"published"}, f"unguarded Listing.objects.* : {calls}"
        )
