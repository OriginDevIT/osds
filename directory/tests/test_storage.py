"""directory.storage -- the backend registry and the per-tenant resolver. No
cloud SDK or account is needed: the backend classes are replaced with recording
fakes at the import seam."""

from __future__ import annotations

import functools
import io
import json
import shutil
import tempfile
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.test import Client, TestCase, override_settings
from django.urls import reverse as _reverse
from django.utils import timezone

from directory import storage as storage_mod
from directory.models import Listing, ListingType, MediaAsset
from directory.storage import (
    StorageConfigError,
    build_storage,
    clear_storage_cache,
    get_tenant_storage,
)
from tenants.models import InstallSetup, Secret, Tenant
from tenants.secrets import set_secret

reverse = functools.partial(_reverse, urlconf="osds.urls_tenant")
SA_JSON = json.dumps({"private_key": "PK", "client_email": "svc@example.test", "project_id": "p"})


class FakeBackend(Storage):
    """Records its construction kwargs; keeps objects in a dict."""

    instances: "list[FakeBackend]" = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.objects: "dict[str, bytes]" = {}
        FakeBackend.instances.append(self)

    def _save(self, name, content):
        self.objects[name] = content.read()
        return name

    def _open(self, name, mode="rb"):
        if name not in self.objects:
            raise FileNotFoundError(name)
        return ContentFile(self.objects[name], name=name)

    def exists(self, name):
        return name in self.objects

    def delete(self, name):
        self.objects.pop(name, None)

    def listdir(self, path):
        files = [k.rsplit("/", 1)[-1] for k in self.objects if k.rsplit("/", 1)[0] == path]
        return [], files


def _fake_import(path, backend):
    if path.endswith("Credentials"):
        return mock.Mock(from_service_account_info=lambda info: ("creds", info["client_email"]))
    return FakeBackend


class _Mixin:
    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="osds-storage-test-")
        self.addCleanup(shutil.rmtree, self._root, ignore_errors=True)
        ctx = override_settings(OSDS_MEDIA_ROOT=self._root)
        ctx.enable()
        self.addCleanup(ctx.disable)
        clear_storage_cache()
        self.addCleanup(clear_storage_cache)
        FakeBackend.instances = []
        self._real_import_class = storage_mod._import_class
        patch = mock.patch.object(storage_mod, "_import_class", _fake_import)
        patch.start()
        self.addCleanup(patch.stop)
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def configure(self, cfg, **secrets):
        self.tenant.settings = {"storage": cfg}
        self.tenant.save()
        for name, value in secrets.items():
            set_secret(f"storage_{name}", value, tenant=self.tenant)


class _Base(_Mixin, TestCase):
    pass


class ResolverTests(_Base):
    def test_s3_class_kwargs_prefix_and_secrets(self):
        self.configure(
            {"backend": "s3", "bucket": "b", "region": "r1", "endpoint": "https://s3.example.test"},
            access_key="AK", secret_key="SK",
        )
        kw = get_tenant_storage(self.tenant).kwargs
        self.assertEqual(kw["bucket_name"], "b")
        self.assertEqual(kw["region_name"], "r1")
        self.assertEqual(kw["endpoint_url"], "https://s3.example.test")
        self.assertEqual((kw["access_key"], kw["secret_key"]), ("AK", "SK"))
        self.assertEqual(kw["location"], f"{self.tenant.public_id}/")
        self.assertFalse(kw["file_overwrite"])

    def test_s3_region_is_optional(self):
        self.configure({"backend": "s3", "bucket": "b"}, access_key="a", secret_key="s")
        self.assertIsNone(get_tenant_storage(self.tenant).kwargs["region_name"])

    def test_s3_endpoint_blank_means_none(self):
        self.configure({"backend": "s3", "bucket": "b", "region": "r"}, access_key="a", secret_key="s")
        self.assertIsNone(get_tenant_storage(self.tenant).kwargs["endpoint_url"])

    def test_azure_kwargs(self):
        self.configure(
            {"backend": "azure", "bucket": "cont", "account_name": "acct"}, account_key="KEY",
        )
        kw = get_tenant_storage(self.tenant).kwargs
        self.assertEqual(
            (kw["azure_container"], kw["account_name"], kw["account_key"]), ("cont", "acct", "KEY")
        )
        self.assertEqual(kw["location"], f"{self.tenant.public_id}/")
        self.assertFalse(kw["overwrite_files"])

    def test_gcp_kwargs(self):
        self.configure({"backend": "gcp", "bucket": "gb"}, service_account_json=SA_JSON)
        kw = get_tenant_storage(self.tenant).kwargs
        self.assertEqual(kw["bucket_name"], "gb")
        self.assertEqual(kw["credentials"], ("creds", "svc@example.test"))
        self.assertEqual(kw["location"], f"{self.tenant.public_id}/")

    def test_legacy_cleartext_access_key_is_the_fallback(self):
        self.configure(
            {"backend": "s3", "bucket": "b", "region": "r", "access_key": "LEGACY"}, secret_key="SK",
        )
        self.assertEqual(get_tenant_storage(self.tenant).kwargs["access_key"], "LEGACY")

    def test_the_secret_wins_over_the_legacy_field(self):
        self.configure(
            {"backend": "s3", "bucket": "b", "region": "r", "access_key": "LEGACY"},
            access_key="NEW", secret_key="SK",
        )
        self.assertEqual(get_tenant_storage(self.tenant).kwargs["access_key"], "NEW")

    def test_instances_are_cached_until_config_or_credentials_change(self):
        self.configure({"backend": "s3", "bucket": "b", "region": "r"}, access_key="a", secret_key="s")
        first = get_tenant_storage(self.tenant)
        self.assertIs(get_tenant_storage(self.tenant), first)
        set_secret("storage_secret_key", "rotated", tenant=self.tenant)
        second = get_tenant_storage(self.tenant)
        self.assertIsNot(second, first)
        self.assertEqual(second.kwargs["secret_key"], "rotated")
        self.tenant.settings["storage"]["bucket"] = "other"
        self.assertEqual(get_tenant_storage(self.tenant).kwargs["bucket_name"], "other")

    def test_the_cache_is_per_tenant(self):
        self.configure({"backend": "azure", "bucket": "c", "account_name": "a"}, account_key="k")
        other = Tenant.objects.create(slug="beta", name="Beta")
        other.settings = dict(self.tenant.settings)
        other.save()
        set_secret("storage_account_key", "k", tenant=other)
        a, b = get_tenant_storage(self.tenant), get_tenant_storage(other)
        self.assertIsNot(a, b)
        self.assertNotEqual(a.kwargs["location"], b.kwargs["location"])

    def test_error_paths(self):
        cases = {
            "unknown backend": ({"backend": "ftp"}, {}),
            "s3 no bucket": ({"backend": "s3"}, {"access_key": "a", "secret_key": "s"}),
            "s3 no keys": ({"backend": "s3", "bucket": "b", "region": "r"}, {}),
            "azure no account": ({"backend": "azure", "bucket": "c"}, {"account_key": "k"}),
            "gcp bad json": ({"backend": "gcp", "bucket": "g"}, {"service_account_json": "{nope"}),
            "gcp json missing fields": ({"backend": "gcp", "bucket": "g"}, {"service_account_json": "{}"}),
        }
        for label, (cfg, secrets) in cases.items():
            with self.subTest(label):
                Secret.objects.all().delete()
                self.configure(cfg, **secrets)
                with self.assertRaises(StorageConfigError):
                    get_tenant_storage(self.tenant)

    def test_a_missing_sdk_is_a_config_error_with_an_install_hint(self):
        self.configure({"backend": "s3", "bucket": "b", "region": "r"}, access_key="a", secret_key="s")
        with mock.patch.object(storage_mod, "_import_class", self._real_import_class):
            with mock.patch("importlib.import_module", side_effect=ImportError("No module named boto3")):
                with self.assertRaises(StorageConfigError) as cm:
                    get_tenant_storage(self.tenant)
        self.assertIn("boto3", str(cm.exception))

    def test_an_undecryptable_secret_is_a_config_error(self):
        self.configure({"backend": "azure", "bucket": "c", "account_name": "a"}, account_key="k")
        with override_settings(OSDS_SECRET_KEY="a-different-key"):
            with self.assertRaises(StorageConfigError):
                get_tenant_storage(self.tenant)

    def test_config_errors_never_echo_a_credential(self):
        self.configure({"backend": "gcp", "bucket": "g"}, service_account_json="{PRIVATE-X")
        with self.assertRaises(StorageConfigError) as cm:
            get_tenant_storage(self.tenant)
        self.assertNotIn("PRIVATE-X", str(cm.exception))

    def test_keys_are_prefixed_with_the_tenant_on_every_backend(self):
        other = Tenant.objects.create(slug="beta", name="Beta")
        for cfg, secrets in (
            ({"backend": "s3", "bucket": "b"}, {"access_key": "a", "secret_key": "s"}),
            ({"backend": "azure", "bucket": "c", "account_name": "a"}, {"account_key": "k"}),
            ({"backend": "gcp", "bucket": "g"}, {"service_account_json": SA_JSON}),
        ):
            with self.subTest(cfg["backend"]):
                kw = build_storage(other.public_id, cfg, secrets).kwargs
                self.assertEqual(kw["location"], f"{other.public_id}/")

    def test_local_default_and_prefix(self):
        self.tenant.settings = {}
        self.assertIn(self.tenant.public_id, get_tenant_storage(self.tenant).location)


class NormalisationTests(TestCase):
    def test_a_provider_not_found_becomes_file_not_found(self):
        class NotFound(Exception):
            pass

        class Base(Storage):
            def _open(self, name, mode="rb"):
                raise NotFound(name)

        with self.assertRaises(FileNotFoundError):
            storage_mod._normalized(Base)().open("x")

    def test_an_http_404_response_becomes_file_not_found(self):
        class ClientError(Exception):
            response = {"ResponseMetadata": {"HTTPStatusCode": 404}}

        class Base(Storage):
            def _open(self, name, mode="rb"):
                raise ClientError()

        with self.assertRaises(FileNotFoundError):
            storage_mod._normalized(Base)().open("x")

    def test_other_errors_pass_through(self):
        class Base(Storage):
            def _open(self, name, mode="rb"):
                raise PermissionError("denied")

        with self.assertRaises(PermissionError):
            storage_mod._normalized(Base)().open("x")

    def test_a_lazy_handle_for_a_missing_object_is_checked_up_front(self):
        class Lazy(Storage):
            _probe_before_read = True

            def _open(self, name, mode="rb"):
                return io.BytesIO()

            def exists(self, name):
                return False

        with self.assertRaises(FileNotFoundError):
            storage_mod._normalized(Lazy)().open("x")


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class CloudServingTests(_Base):
    """A cloud-backed tenant is served through the app: ``storage.open`` -> response."""

    def setUp(self):
        super().setUp()
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant.primary_domain = "acme.test"
        self.tenant.save()
        self.configure({"backend": "azure", "bucket": "c", "account_name": "a"}, account_key="k")
        lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="b", label_singular="B", label_plural="Bs", path_segment="bs",
        )
        self.listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug="l", name="L", visibility="published",
        )
        self.asset = MediaAsset.all_tenants.create(
            tenant=self.tenant, listing=self.listing, role="gallery", status="ready",
            storage_key="listings/x.png", content_type="image/png", byte_size=3, width=1,
            height=1, checksum_sha256="x" * 64,
        )

    def test_the_object_is_proxied_with_its_content_type(self):
        get_tenant_storage(self.tenant).objects["listings/x.png"] = b"png"
        r = Client().get(f"/media/{self.asset.public_id}", HTTP_HOST="acme.test")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), b"png")
        self.assertEqual(r["Content-Type"], "image/png")

    def test_a_missing_object_is_a_404_not_a_500(self):
        r = Client().get(f"/media/{self.asset.public_id}", HTTP_HOST="acme.test")
        self.assertEqual(r.status_code, 404)

    def test_a_misconfigured_backend_is_not_a_500_on_attach(self):
        self.configure({"backend": "s3", "bucket": "b"})
        from directory import media
        from osds.tenancy import tenant_context

        with tenant_context(self.tenant), self.assertRaises(StorageConfigError):
            media.attach_media(self.listing, role="gallery", upload=_png(), actor={"type": "admin", "id": "op"})


def _png():
    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="PNG")
    return SimpleUploadedFile("x.png", buf.getvalue(), content_type="image/png")
