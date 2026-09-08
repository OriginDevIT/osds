"""directory.sitemaps -- robots.txt and the sitemap index.

The generator runs outside a request: it is called here directly, not through
the test client, and one test asserts it never consults the thread-local
urlconf. Another asserts a sitemap ``<loc>`` is byte-identical to the
``rel=canonical`` the page renders for the same listing.
"""

from __future__ import annotations

import re
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.urls import set_urlconf
from django.utils import timezone

from directory import sitemaps
from directory.models import Category, Listing, ListingType
from directory.search import recompute_search_vector
from osds.tenancy import tenant_context
from tenants.models import InstallSetup, Tenant

HOST = "acme.test"

_LOC = re.compile(r"<loc>([^<]+)</loc>")
_CANONICAL = re.compile(r'<link rel="canonical" href="([^"]+)">')


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class _Base(TestCase):
    verified = True

    def setUp(self):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme",
            name="Acme Directory",
            primary_domain=HOST,
            domain_verified_at=timezone.now() if self.verified else None,
        )
        self.client = Client()

    def get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=HOST, **kw)

    def _type(self, key="business", segment="businesses"):
        return ListingType.all_tenants.create(
            tenant=self.tenant, key=key, label_singular=key.title(),
            label_plural=key.title() + "s", path_segment=segment,
        )

    def _cat(self, lt, slug, *, name=None, order=0, parent=None):
        return Category.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug=slug,
            name=name or slug.title(), order=order, parent=parent,
        )

    def _listing(self, lt, slug, *, name=None, visibility="published", cats=()):
        listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug=slug,
            name=name or slug.title(), visibility=visibility,
        )
        with tenant_context(self.tenant):
            if cats:
                listing.categories.set(cats)
            recompute_search_vector(listing)
        return listing

    def locs(self, xml):
        return _LOC.findall(xml)


class GeneratorWithoutRequestTests(_Base):
    """Required test 1: correct absolute URLs, no thread-local urlconf."""

    def setUp(self):
        super().setUp()
        self.lt = self._type()
        self.plumbers = self._cat(self.lt, "plumbers")
        self.acme = self._listing(
            self.lt, "acme-co", name="Acme Co", cats=[self.plumbers]
        )

    def test_child_builds_absolute_urls_with_no_urlconf_on_the_thread(self):
        set_urlconf(None)
        self.addCleanup(set_urlconf, None)
        # Anything reaching for reverse() would blow up here.
        with mock.patch(
            "django.urls.base.get_urlconf", side_effect=AssertionError("urlconf touched")
        ):
            xml = sitemaps.build_child(self.tenant, "listings", 1)
            index = sitemaps.build_index(self.tenant)
            robots = sitemaps.build_robots(self.tenant)

        self.assertIn("https://acme.test/plumbers/acme-co", self.locs(xml))
        self.assertIn(
            f"<lastmod>{self.acme.updated_at.isoformat()}</lastmod>", xml
        )
        self.assertIn("https://acme.test/sitemaps/listings-1.xml", self.locs(index))
        self.assertIn("Sitemap: https://acme.test/sitemap.xml", robots)


class CanonicalMatchesSitemapTests(_Base):
    """Required test 2: <loc> byte-identical to the page's rel=canonical."""

    def setUp(self):
        super().setUp()
        self.lt = self._type()
        self.low = self._cat(self.lt, "aaa", order=0)
        self.high = self._cat(self.lt, "zzz", order=9)
        self.listing = self._listing(
            self.lt, "multi", name="Multi", cats=[self.low, self.high]
        )

    def test_loc_equals_on_page_canonical(self):
        # Fetch the page at its NON-canonical path.
        resp = self.get("/zzz/multi")
        self.assertEqual(resp.status_code, 200)
        on_page = _CANONICAL.search(resp.content.decode()).group(1)

        xml = sitemaps.build_child(self.tenant, "listings", 1)
        loc = next(l for l in self.locs(xml) if l.endswith("/multi"))

        self.assertEqual(loc, on_page)
        self.assertEqual(loc, "https://acme.test/aaa/multi")


class CanonicalAbsoluteWhenVerifiedTests(_Base):
    """Required test 3: canonical is absolute iff the domain is verified."""

    def setUp(self):
        super().setUp()
        self.lt = self._type()
        self.cat = self._cat(self.lt, "plumbers")
        for i in range(3):
            self._listing(self.lt, f"p{i}", name=f"P{i}", cats=[self.cat])

    def _canon(self, path):
        return _CANONICAL.search(self.get(path).content.decode()).group(1)

    def test_absolute_with_a_verified_domain(self):
        self.assertEqual(self._canon("/plumbers/p0"), "https://acme.test/plumbers/p0")
        self.assertEqual(self._canon("/plumbers/"), "https://acme.test/plumbers/")

    def test_relative_without_a_verified_domain(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(domain_verified_at=None)
        self.assertEqual(self._canon("/plumbers/p0"), "/plumbers/p0")
        self.assertEqual(self._canon("/plumbers/"), "/plumbers/")


class SitemapIndexTests(_Base):
    def setUp(self):
        super().setUp()
        self.lt = self._type()
        self.fat = self._cat(self.lt, "fat")
        self.thin = self._cat(self.lt, "thin")
        for i in range(3):
            self._listing(self.lt, f"f{i}", name=f"F{i}", cats=[self.fat])
        self._listing(self.lt, "t0", name="T0", cats=[self.thin])

    def test_index_is_always_a_sitemapindex_with_listings_child(self):
        xml = sitemaps.build_index(self.tenant)
        self.assertIn("<sitemapindex", xml)
        self.assertIn("https://acme.test/sitemaps/listings-1.xml", self.locs(xml))

    def test_index_includes_categories_child_only_when_one_qualifies(self):
        xml = sitemaps.build_index(self.tenant)
        self.assertIn("https://acme.test/sitemaps/categories-1.xml", self.locs(xml))

    def test_no_categories_child_when_none_qualify(self):
        Listing.all_tenants.filter(slug__startswith="f").update(visibility="draft")
        xml = sitemaps.build_index(self.tenant)
        self.assertNotIn(
            "https://acme.test/sitemaps/categories-1.xml", self.locs(xml)
        )
        self.assertIsNone(sitemaps.build_child(self.tenant, "categories", 1))

    def test_empty_tenant_still_has_a_listings_child_with_home(self):
        Listing.all_tenants.all().delete()
        xml = sitemaps.build_index(self.tenant)
        self.assertIn("https://acme.test/sitemaps/listings-1.xml", self.locs(xml))
        child = sitemaps.build_child(self.tenant, "listings", 1)
        self.assertEqual(self.locs(child), ["https://acme.test/"])


class SitemapChildContentTests(_Base):
    def setUp(self):
        super().setUp()
        self.lt = self._type()
        self.low = self._cat(self.lt, "aaa", order=0)
        self.high = self._cat(self.lt, "zzz", order=9)
        self.a = self._listing(self.lt, "a", name="A", cats=[self.low, self.high])
        self.b = self._listing(self.lt, "b", name="B", cats=[self.high])
        self.draft = self._listing(
            self.lt, "d", name="D", visibility="draft", cats=[self.low]
        )
        for i in range(3):
            self._listing(self.lt, f"x{i}", name=f"X{i}", cats=[self.low])

    def test_listings_child_has_home_first_without_lastmod(self):
        xml = sitemaps.build_child(self.tenant, "listings", 1)
        first_url = xml.split("<url>")[1]
        self.assertIn("<loc>https://acme.test/</loc>", first_url)
        self.assertNotIn("<lastmod>", first_url)

    def test_published_listing_appears_at_its_canonical_path_only(self):
        locs = self.locs(sitemaps.build_child(self.tenant, "listings", 1))
        self.assertIn("https://acme.test/aaa/a", locs)
        self.assertNotIn("https://acme.test/zzz/a", locs)

    def test_draft_listing_is_absent(self):
        locs = self.locs(sitemaps.build_child(self.tenant, "listings", 1))
        self.assertNotIn("https://acme.test/aaa/d", locs)

    def test_listing_urls_carry_lastmod(self):
        xml = sitemaps.build_child(self.tenant, "listings", 1)
        self.assertIn(
            f"<loc>https://acme.test/aaa/a</loc>"
            f"<lastmod>{self.a.updated_at.isoformat()}</lastmod>",
            xml,
        )

    def test_categories_child_lists_fat_only(self):
        locs = self.locs(sitemaps.build_child(self.tenant, "categories", 1))
        self.assertEqual(locs, ["https://acme.test/aaa/"])

    def test_unknown_kind_and_bad_shard_return_none(self):
        self.assertIsNone(sitemaps.build_child(self.tenant, "pages", 1))
        self.assertIsNone(sitemaps.build_child(self.tenant, "listings", 0))
        self.assertIsNone(sitemaps.build_child(self.tenant, "listings", 99))


class SitemapShardingTests(_Base):
    def setUp(self):
        super().setUp()
        self.lt = self._type()
        self.cat = self._cat(self.lt, "plumbers")
        for i in range(5):
            self._listing(self.lt, f"n{i}", name=f"N{i}", cats=[self.cat])

    def test_shards_at_the_configured_size(self):
        with mock.patch.object(sitemaps, "SITEMAP_SHARD_SIZE", 2):
            index = sitemaps.build_index(self.tenant)
            # 5 listings + home = 6 entries -> ceil(6/2) = 3 shards
            self.assertEqual(
                [l for l in self.locs(index) if "listings-" in l],
                [
                    "https://acme.test/sitemaps/listings-1.xml",
                    "https://acme.test/sitemaps/listings-2.xml",
                    "https://acme.test/sitemaps/listings-3.xml",
                ],
            )
            self.assertEqual(len(self.locs(sitemaps.build_child(self.tenant, "listings", 2))), 2)
            self.assertEqual(len(self.locs(sitemaps.build_child(self.tenant, "listings", 3))), 2)
            self.assertIsNone(sitemaps.build_child(self.tenant, "listings", 4))


class SitemapViewTests(_Base):
    def setUp(self):
        super().setUp()
        self.lt = self._type()
        self.cat = self._cat(self.lt, "plumbers")
        self._listing(self.lt, "acme-co", name="Acme Co", cats=[self.cat])

    def test_index_view_serves_xml_with_cache_header(self):
        resp = self.get("/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/xml; charset=utf-8")
        self.assertEqual(resp["Cache-Control"], "public, max-age=3600")
        self.assertIn(b"<sitemapindex", resp.content)

    def test_child_view_serves_xml(self):
        resp = self.get("/sitemaps/listings-1.xml")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"https://acme.test/plumbers/acme-co", resp.content)

    def test_child_view_404s_out_of_range(self):
        self.assertEqual(self.get("/sitemaps/listings-9.xml").status_code, 404)

    def test_bad_child_paths_do_not_route(self):
        self.assertEqual(self.get("/sitemaps/listings-0.xml").status_code, 404)
        self.assertEqual(self.get("/sitemaps/pages-1.xml").status_code, 404)

    def test_robots_txt_has_sitemap_and_disallows(self):
        body = self.get("/robots.txt").content.decode()
        self.assertIn("Sitemap: https://acme.test/sitemap.xml", body)
        self.assertIn("Disallow: /admin", body)
        self.assertIn("Disallow: /search", body)
        self.assertNotIn("/media", body)


class NoVerifiedDomainTests(_Base):
    verified = False

    def setUp(self):
        super().setUp()
        self.lt = self._type()
        self.cat = self._cat(self.lt, "plumbers")
        self._listing(self.lt, "acme-co", name="Acme Co", cats=[self.cat])

    def test_sitemap_paths_404(self):
        self.assertEqual(self.get("/sitemap.xml").status_code, 404)
        self.assertEqual(self.get("/sitemaps/listings-1.xml").status_code, 404)

    def test_robots_txt_disallows_everything_with_no_sitemap_line(self):
        self.assertEqual(
            self.get("/robots.txt").content.decode(),
            "User-agent: *\nDisallow: /\n",
        )
