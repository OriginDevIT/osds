"""directory.media -- attach/detach against per-tenant storage.

Covers: validation and EXIF stripping in-request, the asset lifecycle, the
JSON Patch each operation produces (whole-array replace for the gallery),
``listing.updated`` as the only event, the local storage resolver and its
deferred-feature error for cloud backends, and the tenant-scoped media view.
"""

from __future__ import annotations

import io
import shutil
import tempfile

from django.test import Client, TestCase, override_settings
from django.utils import timezone
from PIL import Image

from audit.models import OutboxEvent
from directory import media
from directory.models import Listing, ListingType, MediaAsset
from directory.storage import DeferredFeatureError, get_tenant_storage
from osds.tenancy import tenant_context
from tenants.models import InstallSetup, Tenant

ACTOR = {"type": "admin", "id": "op_test"}
HOST = "acme.test"


def _upload(name="x.png", *, fmt="PNG", size=(48, 32), color=(20, 40, 60), exif=None):
    from django.core.files.uploadedfile import SimpleUploadedFile

    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    if exif is not None:
        img.save(buf, format=fmt, exif=exif)
    else:
        img.save(buf, format=fmt)
    ct = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(fmt, "application/octet-stream")
    return SimpleUploadedFile(name, buf.getvalue(), content_type=ct)


class _Base(TestCase):
    def setUp(self):
        self._media_root = tempfile.mkdtemp(prefix="osds-media-test-")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        self._ctx = override_settings(OSDS_MEDIA_ROOT=self._media_root)
        self._ctx.enable()
        self.addCleanup(self._ctx.disable)

        self.tenant = Tenant.objects.create(slug="acme", name="Acme")
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )
        self.listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="acme-co", name="Acme Co",
            visibility="published",
        )

    def attach(self, **kw):
        kw.setdefault("role", "gallery")
        kw.setdefault("upload", _upload())
        kw.setdefault("actor", ACTOR)
        with tenant_context(self.tenant):
            return media.attach_media(self.listing, **kw)

    def detach(self, asset):
        with tenant_context(self.tenant):
            media.detach_media(asset, actor=ACTOR)

    def patches(self):
        return [
            e.data["changes"]
            for e in OutboxEvent.all_tenants.filter(
                subject=self.listing.public_id, type="listing.updated"
            ).order_by("id")
        ]

    def reload_media(self):
        self.listing.refresh_from_db()
        return self.listing.media


class AttachTests(_Base):
    def test_logo_attach_marks_ready_and_projects_a_ref(self):
        asset = self.attach(role="logo", alt_text="the mark")
        self.assertEqual(asset.status, MediaAsset.Status.READY)
        self.assertTrue(asset.storage_key)
        self.assertEqual(asset.width, 48)
        self.assertEqual(asset.height, 32)

        ref = self.reload_media()["logo"]
        self.assertEqual(ref["asset_id"], asset.public_id)
        self.assertEqual(ref["url"], f"/media/{asset.public_id}")
        self.assertEqual(ref["alt"], "the mark")
        self.assertEqual((ref["width"], ref["height"]), (48, 32))

    def test_logo_attach_emits_only_listing_updated_with_an_add_patch(self):
        asset = self.attach(role="logo")
        types = set(
            OutboxEvent.all_tenants.filter(
                subject=self.listing.public_id
            ).values_list("type", flat=True)
        )
        self.assertEqual(types, {"listing.updated"})
        self.assertEqual(
            self.patches()[-1],
            [{
                "op": "add",
                "path": "/media/logo",
                "value": {
                    "asset_id": asset.public_id,
                    "url": f"/media/{asset.public_id}",
                    "width": 48,
                    "height": 32,
                    "alt": None,
                },
            }],
        )
        event = OutboxEvent.all_tenants.get(type="listing.updated")
        self.assertEqual(event.data["type"], "business")

    def test_replacing_a_logo_discards_the_previous_asset_and_file(self):
        first = self.attach(role="logo")
        storage = get_tenant_storage(self.tenant)
        self.assertTrue(storage.exists(first.storage_key))

        second = self.attach(role="logo")
        self.assertFalse(
            MediaAsset.all_tenants.filter(pk=first.pk).exists()
        )
        self.assertFalse(storage.exists(first.storage_key))
        self.assertEqual(self.reload_media()["logo"]["asset_id"], second.public_id)
        self.assertEqual(self.patches()[-1][0]["op"], "replace")

    def test_gallery_is_whole_array_replace(self):
        a = self.attach(role="gallery")
        self.assertEqual(
            self.patches()[-1],
            [{"op": "replace", "path": "/media/gallery",
              "value": [{"asset_id": a.public_id, "url": f"/media/{a.public_id}",
                         "width": 48, "height": 32, "alt": None}]}],
        )
        b = self.attach(role="gallery")
        last = self.patches()[-1]
        self.assertEqual(last[0]["op"], "replace")
        self.assertEqual(last[0]["path"], "/media/gallery")
        self.assertEqual([r["asset_id"] for r in last[0]["value"]],
                         [a.public_id, b.public_id])
        self.assertEqual(
            [r["asset_id"] for r in self.reload_media()["gallery"]],
            [a.public_id, b.public_id],
        )

    def test_exif_is_stripped_from_the_stored_bytes(self):
        exif = Image.Exif()
        exif[0x010F] = "SecretCameraMake"
        exif[0x9286] = "private note"
        asset = self.attach(role="logo", upload=_upload("p.jpg", fmt="JPEG", exif=exif))

        data = get_tenant_storage(self.tenant).open(asset.storage_key).read()
        reloaded = Image.open(io.BytesIO(data))
        self.assertEqual(dict(reloaded.getexif()), {})
        self.assertIsNone(reloaded.info.get("exif"))

    def test_a_non_image_is_rejected_with_no_asset_and_no_event(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile("notes.png", b"this is not an image", content_type="image/png")
        with self.assertRaises(media.MediaError):
            self.attach(role="logo", upload=bad)
        self.assertEqual(MediaAsset.all_tenants.count(), 0)
        self.assertEqual(OutboxEvent.all_tenants.count(), 0)

    def test_an_unsupported_format_is_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        buf = io.BytesIO()
        Image.new("P", (10, 10)).save(buf, format="GIF")
        gif = SimpleUploadedFile("x.gif", buf.getvalue(), content_type="image/gif")
        with self.assertRaises(media.MediaError):
            self.attach(role="gallery", upload=gif)

    @override_settings(OSDS_MEDIA_MAX_BYTES=64)
    def test_oversize_is_rejected(self):
        with self.assertRaises(media.MediaError):
            self.attach(role="gallery")

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(media.MediaError):
            self.attach(role="banner")


class DetachTests(_Base):
    def test_detach_logo_emits_a_remove_patch_and_drops_the_file(self):
        asset = self.attach(role="logo")
        storage = get_tenant_storage(self.tenant)
        key = asset.storage_key

        self.detach(asset)
        self.assertIsNone(self.reload_media()["logo"])
        self.assertFalse(MediaAsset.all_tenants.filter(pk=asset.pk).exists())
        self.assertFalse(storage.exists(key))
        self.assertEqual(
            self.patches()[-1], [{"op": "remove", "path": "/media/logo"}]
        )

    def test_detach_last_gallery_image_replaces_with_empty_list(self):
        asset = self.attach(role="gallery")
        self.detach(asset)
        self.assertEqual(self.reload_media()["gallery"], [])
        self.assertEqual(
            self.patches()[-1],
            [{"op": "replace", "path": "/media/gallery", "value": []}],
        )

    def test_detach_one_of_several_gallery_images(self):
        a = self.attach(role="gallery")
        b = self.attach(role="gallery")
        self.detach(a)
        self.assertEqual(
            [r["asset_id"] for r in self.reload_media()["gallery"]], [b.public_id]
        )
        self.assertEqual(self.patches()[-1][0]["op"], "replace")


class RefUrlTests(_Base):
    def test_ref_url_is_relative_without_a_verified_domain(self):
        asset = self.attach(role="logo")
        rel = f"/media/{asset.public_id}"
        self.assertEqual(self.reload_media()["logo"]["url"], rel)
        self.assertEqual(self.patches()[-1][0]["value"]["url"], rel)

    def test_ref_url_is_absolute_https_with_a_verified_domain(self):
        self.tenant.primary_domain = "acme.test"
        self.tenant.domain_verified_at = timezone.now()
        self.tenant.save(update_fields=["primary_domain", "domain_verified_at"])

        asset = self.attach(role="logo")
        absolute = f"https://acme.test/media/{asset.public_id}"
        self.assertEqual(self.reload_media()["logo"]["url"], absolute)
        # the value that goes into the durable outbox record
        self.assertEqual(self.patches()[-1][0]["value"]["url"], absolute)

    def test_unverified_domain_stays_relative(self):
        self.tenant.primary_domain = "acme.test"  # set but not verified
        self.tenant.save(update_fields=["primary_domain"])
        asset = self.attach(role="gallery")
        self.assertEqual(
            self.reload_media()["gallery"][0]["url"], f"/media/{asset.public_id}"
        )


class StorageResolverTests(_Base):
    def test_local_backend_is_rooted_per_tenant(self):
        storage = get_tenant_storage(self.tenant)
        self.assertIn(self.tenant.public_id, storage.location)

    def test_cloud_backends_raise_deferred_feature(self):
        for backend in ("s3", "azure", "gcp"):
            self.tenant.settings = {"storage": {"backend": backend}}
            with self.subTest(backend=backend):
                with self.assertRaises(DeferredFeatureError):
                    get_tenant_storage(self.tenant)

    def test_missing_config_defaults_to_local(self):
        self.tenant.settings = {}
        storage = get_tenant_storage(self.tenant)
        self.assertIn(self.tenant.public_id, storage.location)


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class MediaViewTests(TestCase):
    def setUp(self):
        self._media_root = tempfile.mkdtemp(prefix="osds-media-view-")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        self._ctx = override_settings(OSDS_MEDIA_ROOT=self._media_root)
        self._ctx.enable()
        self.addCleanup(self._ctx.disable)

        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain=HOST
        )
        self.other = Tenant.objects.create(
            slug="beta", name="Beta", primary_domain="beta.test"
        )
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )
        self.listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="acme-co", name="Acme Co",
            visibility="published",
        )
        with tenant_context(self.tenant):
            self.asset = media.attach_media(
                self.listing, role="gallery", upload=_upload(), actor=ACTOR
            )

    def test_serves_the_asset_on_its_own_tenant_host(self):
        resp = Client().get(f"/media/{self.asset.public_id}", HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertTrue(resp.has_header("Cache-Control"))

    def test_another_tenant_host_cannot_fetch_it(self):
        resp = Client().get(
            f"/media/{self.asset.public_id}", HTTP_HOST="beta.test"
        )
        self.assertEqual(resp.status_code, 404)

    def test_unknown_id_is_404(self):
        resp = Client().get("/media/media_00000000000000000000000000", HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 404)

    def test_asset_of_an_unpublished_listing_is_not_served(self):
        Listing.all_tenants.filter(pk=self.listing.pk).update(visibility="draft")
        resp = Client().get(f"/media/{self.asset.public_id}", HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 404)

    def test_pending_asset_is_not_served(self):
        with tenant_context(self.tenant):
            pending = MediaAsset.objects.create(
                tenant=self.tenant, listing=self.listing, role="gallery",
                status=MediaAsset.Status.PENDING, storage_key="nope.png",
            )
        resp = Client().get(f"/media/{pending.public_id}", HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 404)

    def test_listing_detail_renders_the_image(self):
        with tenant_context(self.tenant):
            from directory.search import recompute_search_vector

            cat_listing = self.listing
            from directory.models import Category

            cat = Category.objects.create(
                tenant=self.tenant, listing_type=self.lt, slug="plumbers", name="Plumbers"
            )
            cat_listing.categories.set([cat])
            recompute_search_vector(cat_listing)

        resp = Client().get("/plumbers/acme-co", HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"/media/{self.asset.public_id}")
