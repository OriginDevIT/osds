"""The real ``S3Storage`` (django-storages + boto3) built by the registry.

The boto3 resource is replaced with a mock so no network is touched: these
tests assert the SDK calls that each storage operation makes. The MinIO tier at
the bottom runs the same operations against a real S3 API and is skipped unless
``OSDS_TEST_S3_ENDPOINT`` (plus its key pair and bucket) is set.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from unittest import mock

from botocore.exceptions import ClientError
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from storages.backends.s3 import S3Storage

from directory import storage as storage_mod
from directory.models import Listing, ListingType, MediaAsset
from directory.storage import (
    StorageConfigError,
    check_storage,
    clear_storage_cache,
    get_tenant_storage,
)
from tenants.models import InstallSetup, Tenant
from tenants.secrets import set_secret

BUCKET = "osds-secret-bucket-name"
ACCESS = "AKIATESTACCESSKEYID"
SECRET = "test-secret-access-key-value"


def _client_error(status: int, code: str = "404") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "Op",
    )


class S3Base(TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(prefix="osds-s3-test-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        ctx = override_settings(OSDS_MEDIA_ROOT=root)
        ctx.enable()
        self.addCleanup(ctx.disable)
        clear_storage_cache()
        self.addCleanup(clear_storage_cache)
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def configure(self, cfg=None, secrets=True):
        self.tenant.settings = {"storage": {"backend": "s3", "bucket": BUCKET, **(cfg or {})}}
        self.tenant.save()
        if secrets:
            set_secret("storage_access_key", ACCESS, tenant=self.tenant)
            set_secret("storage_secret_key", SECRET, tenant=self.tenant)

    def storage(self):
        """The tenant's real S3Storage with its boto3 resource mocked."""
        st = get_tenant_storage(self.tenant)
        conn = mock.MagicMock(name="s3-resource")
        st._connections.connection = conn
        self.conn = conn
        self.client = conn.meta.client
        self.bucket = conn.Bucket.return_value
        return st


class FactoryTests(S3Base):
    def test_builds_the_real_s3storage_with_explicit_kwargs(self):
        self.configure({"region": "eu-west-2", "endpoint": "https://s3.example.test"})
        st = get_tenant_storage(self.tenant)
        self.assertIsInstance(st, S3Storage)
        self.assertEqual(st.bucket_name, BUCKET)
        self.assertEqual(st.region_name, "eu-west-2")
        self.assertEqual(st.endpoint_url, "https://s3.example.test")
        self.assertEqual(st.access_key, ACCESS)
        self.assertEqual(st.secret_key, SECRET)
        self.assertEqual(st.location, f"{self.tenant.public_id}/")
        self.assertFalse(st.file_overwrite)
        self.assertIsNone(st.default_acl)

    def test_region_and_endpoint_are_optional(self):
        self.configure()
        st = get_tenant_storage(self.tenant)
        self.assertIsNone(st.region_name)
        self.assertIsNone(st.endpoint_url)

    def test_legacy_cleartext_access_key_is_the_fallback(self):
        self.configure({"access_key": "LEGACYKEY"}, secrets=False)
        set_secret("storage_secret_key", SECRET, tenant=self.tenant)
        self.assertEqual(get_tenant_storage(self.tenant).access_key, "LEGACYKEY")

    def test_the_stored_secret_wins_over_the_legacy_field(self):
        self.configure({"access_key": "LEGACYKEY"})
        self.assertEqual(get_tenant_storage(self.tenant).access_key, ACCESS)

    def test_cache_is_reused_then_invalidated_on_config_change(self):
        self.configure()
        first = get_tenant_storage(self.tenant)
        self.assertIs(first, get_tenant_storage(self.tenant))
        self.tenant.settings["storage"]["region"] = "us-west-2"
        self.tenant.save()
        second = get_tenant_storage(self.tenant)
        self.assertIsNot(first, second)
        self.assertEqual(second.region_name, "us-west-2")
        set_secret("storage_secret_key", "rotated", tenant=self.tenant)
        self.assertEqual(get_tenant_storage(self.tenant).secret_key, "rotated")

    def test_a_missing_sdk_is_a_config_error_at_the_import_seam(self):
        self.configure()
        with mock.patch("importlib.import_module", side_effect=ImportError("No module named boto3")):
            with self.assertRaises(StorageConfigError) as cm:
                get_tenant_storage(self.tenant)
        self.assertIn("boto3", str(cm.exception))
        self.assertNotIn(SECRET, str(cm.exception))

    def test_a_missing_bucket_or_credential_is_a_config_error(self):
        self.tenant.settings = {"storage": {"backend": "s3"}}
        self.tenant.save()
        with self.assertRaises(StorageConfigError):
            get_tenant_storage(self.tenant)
        self.configure(secrets=False)
        with self.assertRaises(StorageConfigError):
            get_tenant_storage(self.tenant)


class ClientCallTests(S3Base):
    def setUp(self):
        super().setUp()
        self.configure()
        self.st = self.storage()
        self.prefix = f"{self.tenant.public_id}/"

    def test_save_uploads_under_the_tenant_prefix(self):
        self.client.head_object.side_effect = _client_error(404)  # name is free
        name = self.st.save("listings/a.png", ContentFile(b"png"))
        self.assertEqual(name, "listings/a.png")
        self.conn.Bucket.assert_called_with(BUCKET)
        self.bucket.Object.assert_called_with(f"{self.prefix}listings/a.png")
        self.bucket.Object.return_value.upload_fileobj.assert_called_once()

    def test_a_key_cannot_escape_the_tenant_prefix(self):
        from django.core.exceptions import SuspiciousOperation

        with self.assertRaises(SuspiciousOperation):
            self.st.save("../other/a.png", ContentFile(b"x"))

    def test_open_loads_metadata_then_downloads(self):
        def download(fileobj, **kw):
            fileobj.write(b"hello")

        self.bucket.Object.return_value.download_fileobj.side_effect = download
        with self.st.open("listings/a.png", "rb") as fh:
            self.assertEqual(fh.read(), b"hello")
        self.bucket.Object.assert_called_with(f"{self.prefix}listings/a.png")
        self.bucket.Object.return_value.load.assert_called_once()

    def test_open_of_a_missing_key_is_file_not_found(self):
        self.bucket.Object.return_value.load.side_effect = _client_error(404)
        with self.assertRaises(FileNotFoundError):
            self.st.open("listings/missing.png", "rb")

    def test_open_of_a_missing_key_via_a_raw_provider_error_is_normalised(self):
        # A NoSuchKey surfaced without the 404 metadata still maps to 404.
        err = type("NoSuchKey", (Exception,), {})()
        with mock.patch.object(S3Storage, "_open", side_effect=err):
            with self.assertRaises(FileNotFoundError):
                self.st.open("k", "rb")

    def test_open_other_errors_are_not_swallowed(self):
        self.bucket.Object.return_value.load.side_effect = _client_error(403, "AccessDenied")
        with self.assertRaises(ClientError):
            self.st.open("k", "rb")

    def test_delete_calls_object_delete_and_ignores_404(self):
        self.st.delete("listings/a.png")
        self.bucket.Object.assert_called_with(f"{self.prefix}listings/a.png")
        self.bucket.Object.return_value.delete.assert_called_once()
        self.bucket.Object.return_value.delete.side_effect = _client_error(404)
        self.st.delete("listings/gone.png")  # does not raise

    def test_exists_uses_head_object(self):
        self.assertTrue(self.st.exists("a.png"))
        self.client.head_object.assert_called_once_with(Bucket=BUCKET, Key=f"{self.prefix}a.png")
        self.client.head_object.side_effect = _client_error(404)
        self.assertFalse(self.st.exists("b.png"))

    def test_size_reads_content_length_and_maps_404(self):
        self.bucket.Object.return_value.content_length = 42
        self.assertEqual(self.st.size("a.png"), 42)
        type(self.bucket.Object.return_value).content_length = mock.PropertyMock(
            side_effect=_client_error(404)
        )
        with self.assertRaises(FileNotFoundError):
            self.st.size("a.png")

    def test_listdir_lists_one_level_under_the_prefix(self):
        paginator = self.client.get_paginator.return_value
        paginator.paginate.return_value = [
            {
                "CommonPrefixes": [{"Prefix": f"{self.prefix}sitemaps/g1/"}],
                "Contents": [
                    {"Key": f"{self.prefix}sitemaps/"},
                    {"Key": f"{self.prefix}sitemaps/current.json"},
                ],
            }
        ]
        dirs, files = self.st.listdir("sitemaps")
        self.client.get_paginator.assert_called_once_with("list_objects")
        paginator.paginate.assert_called_once_with(
            Bucket=BUCKET, Delimiter="/", Prefix=f"{self.prefix}sitemaps/"
        )
        self.assertEqual((dirs, files), (["g1"], ["current.json"]))

    def test_listdir_of_an_empty_prefix_is_empty(self):
        self.client.get_paginator.return_value.paginate.return_value = [{}]
        self.assertEqual(self.st.listdir("sitemaps"), ([], []))

    def test_the_probe_passes_against_a_working_client(self):
        store: "dict[str, bytes]" = {}

        def upload(fileobj, **kw):
            store["k"] = fileobj.read()

        def download(fileobj, **kw):
            fileobj.write(store["k"])

        self.client.head_object.side_effect = _client_error(404)
        obj = self.bucket.Object.return_value
        obj.upload_fileobj.side_effect = upload
        obj.download_fileobj.side_effect = download
        self.client.get_paginator.return_value.paginate.side_effect = lambda **kw: [
            {"Contents": [{"Key": self.bucket.Object.call_args_list[0].args[0]}]}
        ]
        check_storage(self.st)
        obj.delete.assert_called_once()


class ServingTests(S3Base):
    def test_a_missing_object_is_a_404_not_a_500(self):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant.primary_domain = "acme.test"
        self.configure()
        lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="b", label_singular="B", label_plural="Bs", path_segment="bs",
        )
        listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug="l", name="L", visibility="published",
        )
        asset = MediaAsset.all_tenants.create(
            tenant=self.tenant, listing=listing, role="gallery", status="ready",
            storage_key="listings/x.png", content_type="image/png", byte_size=3, width=1,
            height=1, checksum_sha256="x" * 64,
        )
        st = self.storage()
        st.bucket  # noqa: B018 - bind the mocked bucket
        self.bucket.Object.return_value.load.side_effect = _client_error(404)
        r = Client().get(f"/media/{asset.public_id}", HTTP_HOST="acme.test")
        self.assertEqual(r.status_code, 404)


_MINIO = {k: os.environ.get(k) for k in (
    "OSDS_TEST_S3_ENDPOINT", "OSDS_TEST_S3_ACCESS_KEY", "OSDS_TEST_S3_SECRET_KEY", "OSDS_TEST_S3_BUCKET",
)}


@mock.patch.dict(os.environ, {}, clear=False)
class MinioTests(S3Base):
    """Real boto3 against an S3-compatible endpoint (the compose ``minio`` service)."""

    def setUp(self):
        super().setUp()
        if not all(_MINIO.values()):
            self.skipTest("OSDS_TEST_S3_* not set; start the minio compose service to run")
        self.tenant.settings = {"storage": {
            "backend": "s3", "bucket": _MINIO["OSDS_TEST_S3_BUCKET"],
            "endpoint": _MINIO["OSDS_TEST_S3_ENDPOINT"], "region": "us-east-1",
        }}
        self.tenant.save()
        set_secret("storage_access_key", _MINIO["OSDS_TEST_S3_ACCESS_KEY"], tenant=self.tenant)
        set_secret("storage_secret_key", _MINIO["OSDS_TEST_S3_SECRET_KEY"], tenant=self.tenant)
        self.st = get_tenant_storage(self.tenant)

    def test_round_trip(self):
        key = f"listings/{uuid.uuid4().hex}.bin"
        self.assertEqual(self.st.save(key, ContentFile(b"abc")), key)
        self.addCleanup(self.st.delete, key)
        self.assertTrue(self.st.exists(key))
        self.assertEqual(self.st.size(key), 3)
        with self.st.open(key, "rb") as fh:
            self.assertEqual(fh.read(), b"abc")
        self.assertIn(key.rsplit("/", 1)[1], self.st.listdir("listings")[1])
        self.st.delete(key)
        self.assertFalse(self.st.exists(key))
        with self.assertRaises(FileNotFoundError):
            self.st.open(key, "rb")

    def test_the_probe_passes(self):
        check_storage(self.st)
