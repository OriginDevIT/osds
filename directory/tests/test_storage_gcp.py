"""The real ``GoogleCloudStorage`` (django-storages + google-cloud-storage) built by the registry.

The bucket and client are replaced with mocks so no network is touched: these
tests assert the SDK calls that each storage operation makes. The emulator tier
at the bottom runs the same operations against a real GCS-compatible endpoint
and is skipped unless ``OSDS_TEST_GCS_*`` is set.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from google.cloud.exceptions import NotFound
from storages.backends.gcloud import GoogleCloudStorage

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
EMAIL = "svc@osds-proj.iam.example.test"


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _sa_json(email: str = EMAIL) -> str:
    return json.dumps({
        "type": "service_account",
        "project_id": "osds-proj",
        "private_key_id": "k1",
        "private_key": _pem(),
        "client_email": email,
        "token_uri": "https://oauth2.example.test/token",
    })


class GcpBase(TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(prefix="osds-gcp-test-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        ctx = override_settings(OSDS_MEDIA_ROOT=root)
        ctx.enable()
        self.addCleanup(ctx.disable)
        clear_storage_cache()
        self.addCleanup(clear_storage_cache)
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def configure(self, cfg=None, secret=True):
        self.tenant.settings = {"storage": {"backend": "gcp", "bucket": BUCKET, **(cfg or {})}}
        self.tenant.save()
        if secret:
            set_secret("storage_service_account_json", _sa_json(), tenant=self.tenant)

    def storage(self):
        """The tenant's real GoogleCloudStorage with its client and bucket mocked."""
        st = get_tenant_storage(self.tenant)
        self.client = mock.MagicMock(name="gcs-client")
        self.bucket = self.client.bucket.return_value
        st._client = self.client
        st._bucket = self.bucket
        return st


class FactoryTests(GcpBase):
    def test_builds_the_real_class_with_explicit_kwargs(self):
        self.configure()
        st = get_tenant_storage(self.tenant)
        self.assertIsInstance(st, GoogleCloudStorage)
        self.assertEqual(st.bucket_name, BUCKET)
        self.assertEqual(st.project_id, "osds-proj")
        self.assertEqual(st.location, f"{self.tenant.public_id}/")
        self.assertFalse(st.file_overwrite)
        self.assertIsNone(st.default_acl)
        self.assertFalse(st.iam_sign_blob)  # stored key only, no ambient identity

    def test_credentials_come_from_the_stored_secret(self):
        self.configure()
        creds = get_tenant_storage(self.tenant).credentials
        self.assertEqual(creds.service_account_email, EMAIL)

    def test_a_secret_stored_in_cleartext_settings_is_not_a_fallback(self):
        # Only the S3 access key ever had a cleartext copy in the settings block.
        self.configure({"service_account_json": _sa_json()}, secret=False)
        with self.assertRaises(StorageConfigError):
            get_tenant_storage(self.tenant)

    def test_the_legacy_s3_fallback_does_not_leak_into_gcp(self):
        self.configure({"access_key": "LEGACY"}, secret=False)
        with self.assertRaises(StorageConfigError):
            get_tenant_storage(self.tenant)

    def test_cache_is_reused_then_invalidated_on_config_change(self):
        self.configure()
        first = get_tenant_storage(self.tenant)
        self.assertIs(first, get_tenant_storage(self.tenant))
        self.tenant.settings["storage"]["bucket"] = "other-bucket"
        self.tenant.save()
        second = get_tenant_storage(self.tenant)
        self.assertIsNot(first, second)
        self.assertEqual(second.bucket_name, "other-bucket")
        set_secret("storage_service_account_json", _sa_json("rotated@p.example.test"), tenant=self.tenant)
        self.assertEqual(
            get_tenant_storage(self.tenant).credentials.service_account_email,
            "rotated@p.example.test",
        )

    def test_a_missing_sdk_is_a_config_error_at_the_import_seam(self):
        self.configure()
        with mock.patch("importlib.import_module", side_effect=ImportError("No module named google")):
            with self.assertRaises(StorageConfigError) as cm:
                get_tenant_storage(self.tenant)
        self.assertIn("google-cloud-storage", str(cm.exception))

    def test_a_missing_bucket_or_credential_is_a_config_error(self):
        self.tenant.settings = {"storage": {"backend": "gcp"}}
        self.tenant.save()
        with self.assertRaises(StorageConfigError):
            get_tenant_storage(self.tenant)
        self.configure(secret=False)  # bucket but no credential stored
        with self.assertRaises(StorageConfigError):
            get_tenant_storage(self.tenant)
        self.configure({"bucket": ""})  # credential stored, no bucket
        with self.assertRaises(StorageConfigError):
            get_tenant_storage(self.tenant)

    def test_bad_credentials_are_a_config_error_that_does_not_echo_the_key(self):
        self.configure(secret=False)
        for raw in ("not json", json.dumps({"client_email": EMAIL}),
                    json.dumps({"private_key": "PRIVKEY-BAD", "client_email": EMAIL})):
            set_secret("storage_service_account_json", raw, tenant=self.tenant)
            with self.assertRaises(StorageConfigError) as cm:
                get_tenant_storage(self.tenant)
            self.assertNotIn("PRIVKEY-BAD", str(cm.exception))


class ClientCallTests(GcpBase):
    def setUp(self):
        super().setUp()
        self.configure()
        self.st = self.storage()
        self.prefix = f"{self.tenant.public_id}/"
        # A free name makes django-storages build a new Blob; keep it off the network.
        patch = mock.patch("storages.backends.gcloud.Blob")
        self.Blob = patch.start()
        self.addCleanup(patch.stop)

    def test_save_uploads_under_the_tenant_prefix(self):
        self.bucket.get_blob.return_value = None  # name is free
        name = self.st.save("listings/a.png", ContentFile(b"png"))
        self.assertEqual(name, "listings/a.png")
        self.assertEqual(self.Blob.call_args.args[0], f"{self.prefix}listings/a.png")
        self.Blob.return_value.upload_from_file.assert_called_once()

    def test_a_key_cannot_escape_the_tenant_prefix(self):
        from django.core.exceptions import SuspiciousOperation

        self.bucket.get_blob.return_value = None
        with self.assertRaises(SuspiciousOperation):
            self.st.save("../other/a.png", ContentFile(b"x"))
        self.Blob.return_value.upload_from_file.assert_not_called()

    def test_open_downloads_the_blob(self):
        blob = self.bucket.get_blob.return_value
        blob.download_to_file.side_effect = lambda f, **kw: f.write(b"hello")
        with self.st.open("listings/a.png", "rb") as fh:
            self.assertEqual(fh.read(), b"hello")
        self.assertEqual(self.bucket.get_blob.call_args.args[0], f"{self.prefix}listings/a.png")

    def test_open_of_a_missing_key_is_file_not_found(self):
        self.bucket.get_blob.return_value = None
        with self.assertRaises(FileNotFoundError):
            self.st.open("listings/missing.png", "rb")

    def test_open_of_a_missing_key_via_a_raw_provider_error_is_normalised(self):
        with mock.patch.object(GoogleCloudStorage, "_open", side_effect=NotFound("no such object")):
            with self.assertRaises(FileNotFoundError):
                self.st.open("k", "rb")

    def test_open_other_errors_are_not_swallowed(self):
        with mock.patch.object(GoogleCloudStorage, "_open", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.st.open("k", "rb")

    def test_delete_calls_delete_blob_and_ignores_404(self):
        self.st.delete("listings/a.png")
        self.assertEqual(
            self.bucket.delete_blob.call_args.args[0], f"{self.prefix}listings/a.png"
        )
        self.bucket.delete_blob.side_effect = NotFound("gone")
        self.st.delete("listings/gone.png")  # does not raise

    def test_exists_uses_get_blob(self):
        self.bucket.get_blob.return_value = object()
        self.assertTrue(self.st.exists("a.png"))
        self.assertEqual(self.bucket.get_blob.call_args.args[0], f"{self.prefix}a.png")
        self.bucket.get_blob.return_value = None
        self.assertFalse(self.st.exists("b.png"))

    def test_size_reads_the_blob_metadata(self):
        self.bucket.get_blob.return_value = SimpleNamespace(size=42)
        self.assertEqual(self.st.size("a.png"), 42)
        self.assertEqual(self.bucket.get_blob.call_args.args[0], f"{self.prefix}a.png")

    def test_size_of_a_missing_blob_is_not_found(self):
        self.bucket.get_blob.return_value = None
        with self.assertRaises(NotFound):
            self.st.size("a.png")

    def _listing(self, blobs, prefixes):
        it = mock.MagicMock()
        it.__iter__.side_effect = lambda: iter(blobs)
        it.prefixes = prefixes
        self.bucket.list_blobs.return_value = it

    def test_listdir_lists_one_level_under_the_tenant_prefix_with_relative_names(self):
        self._listing(
            [SimpleNamespace(name=f"{self.prefix}sitemaps/current.json")],
            {f"{self.prefix}sitemaps/g1/"},
        )
        dirs, files = self.st.listdir("sitemaps")
        self.assertEqual(
            self.bucket.list_blobs.call_args.kwargs,
            {"prefix": f"{self.prefix}sitemaps/", "delimiter": "/"},
        )
        self.assertEqual((dirs, files), (["g1"], ["current.json"]))

    def test_listdir_of_the_root_stays_inside_the_tenant_prefix(self):
        self._listing([], set())
        self.assertEqual(self.st.listdir(""), ([], []))
        self.assertEqual(self.bucket.list_blobs.call_args.kwargs["prefix"], self.prefix)

    def test_the_probe_passes_against_a_working_client(self):
        store: "dict[str, bytes]" = {}

        def blob(name, *a, **kw):
            b = mock.MagicMock(name=name)
            b.name = name

            def upload(content, **kw):
                content.seek(0)
                store[name] = content.read()

            b.upload_from_file.side_effect = upload
            b.download_to_file.side_effect = lambda f, **kw: f.write(store[name])
            return b

        def get_blob(name, **kw):
            return blob(name) if name in store else None

        def list_blobs(prefix="", delimiter=None, **kw):
            it = mock.MagicMock()
            it.__iter__.side_effect = lambda: iter(
                [SimpleNamespace(name=n) for n in store if n.startswith(prefix)
                 and "/" not in n[len(prefix):]]
            )
            it.prefixes = set()
            return it

        self.Blob.side_effect = blob
        self.bucket.get_blob.side_effect = get_blob
        self.bucket.list_blobs.side_effect = list_blobs
        self.bucket.delete_blob.side_effect = lambda name, **kw: store.pop(name, None)
        check_storage(self.st)
        self.assertEqual(store, {})


class ServingTests(GcpBase):
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
        self.storage()
        self.bucket.get_blob.return_value = None
        r = Client().get(f"/media/{asset.public_id}", HTTP_HOST="acme.test")
        self.assertEqual(r.status_code, 404)


_EMU_KEYS = ("OSDS_TEST_GCS_ENDPOINT", "OSDS_TEST_GCS_BUCKET")
_EMU = {k: os.environ.get(k) for k in _EMU_KEYS}


class EmulatorTests(GcpBase):
    """Real google-cloud-storage against a GCS-compatible endpoint (an emulator).

    Skipped unless ``OSDS_TEST_GCS_ENDPOINT`` and ``OSDS_TEST_GCS_BUCKET`` are
    set. The wizard has no endpoint field, so the test points the client at the
    emulator with anonymous credentials. Never run so far: no emulator image is
    available to this project.
    """

    def setUp(self):
        super().setUp()
        if not all(_EMU.values()):
            self.skipTest("OSDS_TEST_GCS_* not set; no emulator is configured")
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import storage

        self.configure({"bucket": _EMU["OSDS_TEST_GCS_BUCKET"]})
        self.st = get_tenant_storage(self.tenant)
        self.st._client = storage.Client(
            project="osds-proj",
            credentials=AnonymousCredentials(),
            client_options={"api_endpoint": _EMU["OSDS_TEST_GCS_ENDPOINT"]},
        )

    def test_round_trip(self):
        st = self.st
        key = f"listings/{uuid.uuid4().hex}.bin"
        self.assertEqual(st.save(key, ContentFile(b"abc")), key)
        self.addCleanup(st.delete, key)
        self.assertTrue(st.exists(key))
        self.assertEqual(st.size(key), 3)
        with st.open(key, "rb") as fh:
            self.assertEqual(fh.read(), b"abc")
        self.assertIn(key.rsplit("/", 1)[1], st.listdir("listings")[1])
        st.delete(key)
        self.assertFalse(st.exists(key))
        with self.assertRaises(FileNotFoundError):
            st.open(key, "rb")

    def test_the_probe_passes(self):
        check_storage(self.st)
