"""directory.sitemaps -- robots.txt and the sitemap index.

The generator runs outside a request: it is called here directly, not through
the test client, and one test asserts it never consults the thread-local
urlconf. Another asserts a sitemap ``<loc>`` is byte-identical to the
``rel=canonical`` the page renders for the same listing.
"""

from __future__ import annotations

import re
import tempfile
from datetime import timedelta
from unittest import mock

from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from django.urls import set_urlconf
from django.utils import timezone

from directory import sitemaps
from directory.jobs import sitemap_regeneration
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


# ---------------------------------------------------------------------------
# #159: stored artifact, fingerprint, debounce, serving
# ---------------------------------------------------------------------------
class _StoredBase(_Base):
    def setUp(self):
        root = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(root.cleanup)
        self.enterContext(override_settings(OSDS_MEDIA_ROOT=root.name))
        super().setUp()
        self.now = timezone.now()
        self.lt = self._type()
        self.cat = self._cat(self.lt, "plumbers")
        for i in range(3):  # three published: the category is indexable
            self._listing(self.lt, f"p{i}", cats=[self.cat])

    def pointer(self):
        stored = sitemaps.load(self.tenant)
        return None if stored is None else stored.pointer

    def refresh(self, **delta):
        return sitemaps.refresh(self.tenant, now=self.now + timedelta(**delta))


class RefreshTests(_StoredBase):
    def test_first_refresh_renders_and_stores_every_file(self):
        self.assertEqual(self.refresh(), "rebuilt")
        stored = sitemaps.load(self.tenant)
        self.assertIsNotNone(stored)
        for name in ("index.xml", "listings-1.xml", "categories-1.xml"):
            self.assertIsNotNone(stored.get(name), name)
        self.assertEqual(stored.get("index.xml"), sitemaps.build_index(self.tenant))
        self.assertEqual(
            stored.get("listings-1.xml"),
            sitemaps.build_child(self.tenant, "listings", 1),
        )

    def test_an_unchanged_fingerprint_is_current_and_writes_nothing(self):
        self.refresh()
        first = self.pointer()
        self.assertEqual(self.refresh(minutes=30), "current")
        self.assertEqual(self.pointer(), first)

    def test_a_change_inside_the_window_is_debounced_then_rebuilt(self):
        self.refresh()
        self._listing(self.lt, "late", cats=[self.cat])

        self.assertEqual(self.refresh(minutes=4), "debounced")
        self.assertNotIn("/late", sitemaps.load(self.tenant).get("listings-1.xml"))

        self.assertEqual(self.refresh(minutes=5), "rebuilt")
        self.assertIn("/late", sitemaps.load(self.tenant).get("listings-1.xml"))

    def test_the_previous_generation_is_removed(self):
        self.refresh()
        old = self.pointer()["generation"]
        self._listing(self.lt, "late", cats=[self.cat])
        self.refresh(minutes=6)
        storage = sitemaps.get_tenant_storage(self.tenant)
        directories, _ = storage.listdir("sitemaps")
        self.assertEqual(directories, [self.pointer()["generation"]])
        self.assertNotIn(old, directories)

    def _fingerprint_changes_after(self, mutate):
        before = sitemaps.fingerprint(self.tenant)
        mutate()
        self.assertNotEqual(before, sitemaps.fingerprint(self.tenant))

    def test_fingerprint_sees_a_publish(self):
        self._fingerprint_changes_after(
            lambda: self._listing(self.lt, "new", cats=[self.cat])
        )

    def test_fingerprint_sees_an_unpublish(self):
        def unpublish():
            Listing.all_tenants.filter(slug="p0").update(visibility="draft")

        self._fingerprint_changes_after(unpublish)

    def test_fingerprint_sees_a_publish_paired_with_an_unpublish(self):
        # Same published count, same max(updated_at): only the id sum moves.
        spare = self._listing(self.lt, "spare", visibility="draft", cats=[self.cat])

        def swap():
            Listing.all_tenants.filter(slug="p0").update(visibility="draft")
            Listing.all_tenants.filter(pk=spare.pk).update(visibility="published")

        self._fingerprint_changes_after(swap)

    def test_fingerprint_sees_a_listing_moved_between_categories(self):
        other = self._cat(self.lt, "drains")
        listing = Listing.all_tenants.get(slug="p0")

        def move():
            with tenant_context(self.tenant):
                listing.categories.set([other])

        self._fingerprint_changes_after(move)

    def test_fingerprint_sees_a_category_slug_change(self):
        self._fingerprint_changes_after(
            lambda: Category.all_tenants.filter(pk=self.cat.pk).update(slug="pipes")
        )

    def test_fingerprint_sees_a_path_segment_change(self):
        def change():
            self.lt.path_segment = "trades"
            self.lt.save()

        self._fingerprint_changes_after(change)

    def test_fingerprint_is_stable_when_nothing_changed(self):
        self.assertEqual(
            sitemaps.fingerprint(self.tenant), sitemaps.fingerprint(self.tenant)
        )


class RefreshEdgeTests(_StoredBase):
    def test_an_unverified_domain_gets_no_artifact_and_loses_an_old_one(self):
        self.refresh()
        self.assertIsNotNone(self.pointer())

        self.tenant.domain_verified_at = None
        self.tenant.save()
        self.assertEqual(self.refresh(minutes=1), "cleared")
        self.assertIsNone(sitemaps.load(self.tenant))
        self.assertEqual(self.refresh(minutes=2), "current")  # nothing left to clear

    def test_a_domain_change_invalidates_the_stored_files_until_rebuilt(self):
        self.refresh()
        self.tenant.primary_domain = "new.test"
        self.tenant.save()
        self.assertIsNone(sitemaps.load(self.tenant))  # the locs would name acme.test
        self.assertEqual(self.refresh(minutes=6), "rebuilt")
        self.assertIn("https://new.test/", sitemaps.load(self.tenant).get("index.xml"))

    def test_a_cloud_storage_tenant_is_skipped_not_failed(self):
        self.tenant.settings = {"storage": {"backend": "s3", "bucket": "b"}}
        self.tenant.save()
        self.assertEqual(self.refresh(), "unavailable")
        self.assertFalse(sitemaps.needs_rebuild(self.tenant, now=self.now))
        self.assertIsNone(sitemaps.load(self.tenant))

    def test_refresh_is_idempotent(self):
        self.assertEqual(self.refresh(), "rebuilt")
        self.assertEqual(self.refresh(), "current")
        self.assertEqual(self.refresh(minutes=1), "current")


class StoredServingTests(_StoredBase):
    def test_with_no_artifact_the_view_generates_live(self):
        resp = self.get("/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content.decode(), sitemaps.build_index(self.tenant))

    def test_the_view_serves_the_stored_file_not_a_live_build(self):
        self.refresh()
        storage = sitemaps.get_tenant_storage(self.tenant)
        path = f"sitemaps/{self.pointer()['generation']}/listings-1.xml"
        storage.delete(path)
        storage.save(path, ContentFile(b"<urlset>stored</urlset>"))

        resp = self.get("/sitemaps/listings-1.xml")

        self.assertEqual(resp.content, b"<urlset>stored</urlset>")
        self.assertEqual(resp["Cache-Control"], "public, max-age=3600")
        self.assertIn("application/xml", resp["Content-Type"])

    def test_a_shard_a_stored_generation_lacks_is_a_404_not_a_live_build(self):
        self.refresh()
        self.assertEqual(self.get("/sitemaps/listings-2.xml").status_code, 404)
        self.assertEqual(self.get("/sitemaps/categories-1.xml").status_code, 200)

    def test_the_index_is_served_from_storage(self):
        self.refresh()
        self.assertEqual(
            self.get("/sitemap.xml").content.decode(),
            sitemaps.load(self.tenant).get("index.xml"),
        )

    def test_an_unverified_domain_still_404s_with_a_stored_generation_present(self):
        self.refresh()
        Tenant.objects.filter(pk=self.tenant.pk).update(domain_verified_at=None)
        self.assertEqual(self.get("/sitemap.xml").status_code, 404)


class SitemapJobTests(_StoredBase):
    def _second_tenant(self, slug="beta"):
        tenant = Tenant.objects.create(
            slug=slug, name=slug, primary_domain=f"{slug}.test",
            domain_verified_at=timezone.now(),
        )
        lt = ListingType.all_tenants.create(
            tenant=tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )
        cat = Category.all_tenants.create(
            tenant=tenant, listing_type=lt, slug="c", name="C"
        )
        for i in range(3):
            listing = Listing.all_tenants.create(
                tenant=tenant, listing_type=lt, slug=f"x{i}", name=f"X{i}",
                visibility="published",
            )
            with tenant_context(tenant):
                listing.categories.set([cat])
        return tenant

    def test_one_tenant_rebuilds_per_call_and_more_is_set_while_another_waits(self):
        second = self._second_tenant()

        first = sitemap_regeneration(now=self.now)
        self.assertEqual((first.done, first.more), (1, True))
        self.assertIsNotNone(sitemaps.load(self.tenant))
        self.assertIsNone(sitemaps.load(second))

        next_call = sitemap_regeneration(now=self.now)
        self.assertEqual((next_call.done, next_call.more), (1, False))
        self.assertIsNotNone(sitemaps.load(second))

    def test_an_up_to_date_install_does_nothing(self):
        sitemap_regeneration(now=self.now)
        again = sitemap_regeneration(now=self.now + timedelta(minutes=1))
        self.assertEqual((again.done, again.more), (0, False))

    def test_a_suspended_tenant_is_left_alone(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(status=Tenant.Status.SUSPENDED)
        result = sitemap_regeneration(now=self.now)
        self.assertEqual(result.done, 0)
        self.assertIsNone(sitemaps.load(self.tenant))

    def test_a_cloud_tenant_does_not_fail_the_tick_or_block_others(self):
        self.tenant.settings = {"storage": {"backend": "gcp", "bucket": "b"}}
        self.tenant.save()
        other = self._second_tenant()
        result = sitemap_regeneration(now=self.now)
        self.assertEqual(result.done, 1)
        self.assertIsNotNone(sitemaps.load(other))
