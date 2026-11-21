"""The real ``AzureStorage`` (django-storages + azure-storage-blob) built by the registry.

The container client is replaced with a mock so no network is touched: these
tests assert the SDK calls that each storage operation makes. The emulator tier
at the bottom runs the same operations against a real Blob API and is skipped
unless ``OSDS_TEST_AZURE_CONNECTION`` (plus its container) is set.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from types import SimpleNamespace
from unittest import mock

from azure.core.exceptions import ResourceNotFoundError
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from storages.backends.azure_storage import AzureStorage

from directory.models import Listing, ListingType, MediaAsset
from directory.storage import (
    StorageConfigError,
    check_storage,
    clear_storage_cache,
    get_tenant_storage,
)
from tenants.models import InstallSetup, Tenant
from tenants.secrets import set_secret

CONTAINER = "osds-secret-container-name"
ACCOUNT = "osdsacctname"
KEY = "dGVzdC1hY2NvdW50LWtleS12YWx1ZQ=="


def _not_found() -> ResourceNotFoundError:
    return ResourceNotFoundError("BlobNotFound")


class AzureBase(TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(prefix="osds-azure-test-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        ctx = override_settings(OSDS_MEDIA_ROOT=root)
        ctx.enable()
        self.addCleanup(ctx.disable)
        clear_storage_cache()
        self.addCleanup(clear_storage_cache)
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def configure(self, cfg=None, secrets=True):
        self.tenant.settings = {
            "storage": {"backend": "azure", "bucket": CONTAINER, "account_name": ACCOUNT, **(cfg or {})}
        }
        self.tenant.save()
        if secrets:
            set_secret("storage_account_key", KEY, tenant=self.tenant)

    def storage(self):
        """The tenant's real AzureStorage with its container client mocked."""
        st = get_tenant_storage(self.tenant)
        self.container = mock.MagicMock(name="container-client")
        st._client = self.container
        self.blob = self.container.get_blob_client.return_value
        return st


class FactoryTests(AzureBase):
    def test_builds_the_real_azurestorage_with_explicit_kwargs(self):
        self.configure()
        st = get_tenant_storage(self.tenant)
        self.assertIsInstance(st, AzureStorage)
        self.assertEqual(st.azure_container, CONTAINER)
        self.assertEqual(st.account_name, ACCOUNT)
        self.assertEqual(st.account_key, KEY)
        self.assertEqual(st.location, f"{self.tenant.public_id}/")
        self.assertFalse(st.overwrite_files)
        self.assertTrue(st.azure_ssl)

    def test_the_service_client_is_built_from_the_account_and_key(self):
        self.configure()
        st = get_tenant_storage(self.tenant)
        svc = st.service_client
        self.assertEqual(svc.account_name, ACCOUNT)
        self.assertTrue(svc.url.startswith(f"https://{ACCOUNT}.blob."))

    def test_the_stored_secret_is_used(self):
        self.configure()
        self.assertEqual(get_tenant_storage(self.tenant).account_key, KEY)

    def test_legacy_cleartext_account_key_is_not_a_fallback_for_azure(self):
        # Only the S3 access key ever had a cleartext copy in the settings block.
        self.configure({"account_key": "LEGACYKEY"}, secrets=False)
        with self.assertRaises(StorageConfigError):
            get_tenant_storage(self.tenant)

    def test_cache_is_reused_then_invalidated_on_config_change(self):
        self.configure()
        first = get_tenant_storage(self.tenant)
        self.assertIs(first, get_tenant_storage(self.tenant))
        self.tenant.settings["storage"]["bucket"] = "other-container"
        self.tenant.save()
        second = get_tenant_storage(self.tenant)
        self.assertIsNot(first, second)
        self.assertEqual(second.azure_container, "other-container")
        set_secret("storage_account_key", "rotated", tenant=self.tenant)
        self.assertEqual(get_tenant_storage(self.tenant).account_key, "rotated")

    def test_a_missing_sdk_is_a_config_error_at_the_import_seam(self):
        self.configure()
        with mock.patch(
            "importlib.import_module", side_effect=ImportError("No module named azure")
        ):
            with self.assertRaises(StorageConfigError) as cm:
                get_tenant_storage(self.tenant)
        self.assertIn("azure-storage-blob", str(cm.exception))
        self.assertNotIn(KEY, str(cm.exception))

    def test_a_missing_container_account_or_key_is_a_config_error(self):
        self.tenant.settings = {"storage": {"backend": "azure"}}
        self.tenant.save()
        with self.assertRaises(StorageConfigError):
            get_tenant_storage(self.tenant)
        self.configure({"account_name": ""})
        with self.assertRaises(StorageConfigError):
            get_tenant_storage(self.tenant)
        self.configure({"bucket": ""})
        with self.assertRaises(StorageConfigError):
            get_tenant_storage(self.tenant)

    def test_a_missing_key_is_a_config_error(self):
        self.configure(secrets=False)
        with self.assertRaises(StorageConfigError):
            get_tenant_storage(self.tenant)


class ClientCallTests(AzureBase):
    def setUp(self):
        super().setUp()
        self.configure()
        self.st = self.storage()
        self.prefix = f"{self.tenant.public_id}/"

    def test_save_uploads_under_the_tenant_prefix(self):
        self.blob.exists.return_value = False  # name is free
        name = self.st.save("listings/a.png", ContentFile(b"png"))
        self.assertEqual(name, "listings/a.png")
        self.container.get_blob_client.assert_called_with(f"{self.prefix}listings/a.png")
        args, kwargs = self.container.upload_blob.call_args
        self.assertEqual(args[0], f"{self.prefix}listings/a.png")
        self.assertFalse(kwargs["overwrite"])

    def test_a_key_cannot_escape_the_tenant_prefix(self):
        from django.core.exceptions import SuspiciousOperation

        with self.assertRaises(SuspiciousOperation):
            self.st.save("../other/a.png", ContentFile(b"x"))
        self.container.upload_blob.assert_not_called()

    def test_open_checks_existence_then_downloads(self):
        self.blob.exists.return_value = True
        self.container.download_blob.return_value.readinto.side_effect = lambda f: f.write(b"hello")
        with self.st.open("listings/a.png", "rb") as fh:
            self.assertEqual(fh.read(), b"hello")
        self.container.download_blob.assert_called_once()
        self.assertEqual(
            self.container.download_blob.call_args.args[0], f"{self.prefix}listings/a.png"
        )

    def test_open_of_a_missing_key_is_file_not_found(self):
        self.blob.exists.return_value = False
        with self.assertRaises(FileNotFoundError):
            self.st.open("listings/missing.png", "rb")

    def test_open_of_a_missing_key_via_a_raw_provider_error_is_normalised(self):
        with mock.patch.object(AzureStorage, "_open", side_effect=_not_found()):
            with self.assertRaises(FileNotFoundError):
                self.st.open("k", "rb")

    def test_open_other_errors_are_not_swallowed(self):
        with mock.patch.object(AzureStorage, "_open", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.st.open("k", "rb")

    def test_delete_calls_delete_blob_and_ignores_404(self):
        self.st.delete("listings/a.png")
        self.assertEqual(
            self.container.delete_blob.call_args.args[0], f"{self.prefix}listings/a.png"
        )
        self.container.delete_blob.side_effect = _not_found()
        self.st.delete("listings/gone.png")  # does not raise

    def test_exists_uses_the_blob_client(self):
        self.blob.exists.return_value = True
        self.assertTrue(self.st.exists("a.png"))
        self.container.get_blob_client.assert_called_with(f"{self.prefix}a.png")
        self.blob.exists.return_value = False
        self.assertFalse(self.st.exists("b.png"))

    def test_size_reads_the_blob_properties(self):
        self.blob.get_blob_properties.return_value = SimpleNamespace(size=42)
        self.assertEqual(self.st.size("a.png"), 42)
        self.container.get_blob_client.assert_called_with(f"{self.prefix}a.png")

    def test_listdir_lists_one_level_under_the_tenant_prefix_with_relative_names(self):
        self.container.list_blobs.return_value = [
            SimpleNamespace(name=f"{self.prefix}sitemaps/g1/part-1.xml"),
            SimpleNamespace(name=f"{self.prefix}sitemaps/g1/part-2.xml"),
            SimpleNamespace(name=f"{self.prefix}sitemaps/current.json"),
        ]
        dirs, files = self.st.listdir("sitemaps")
        self.assertEqual(
            self.container.list_blobs.call_args.kwargs["name_starts_with"],
            f"{self.prefix}sitemaps/",
        )
        self.assertEqual((dirs, files), (["g1"], ["current.json"]))

    def test_listdir_of_the_root_stays_inside_the_tenant_prefix(self):
        self.container.list_blobs.return_value = []
        self.assertEqual(self.st.listdir(""), ([], []))
        self.assertEqual(
            self.container.list_blobs.call_args.kwargs["name_starts_with"], self.prefix
        )

    def test_listdir_of_an_empty_prefix_is_empty(self):
        self.container.list_blobs.return_value = []
        self.assertEqual(self.st.listdir("sitemaps"), ([], []))

    def test_the_probe_passes_against_a_working_client(self):
        store: "dict[str, bytes]" = {}

        def upload(name, data, **kw):
            data.seek(0)
            store[name] = data.read()

        def download(name, **kw):
            stream = mock.MagicMock()
            stream.readinto.side_effect = lambda f: f.write(store[name])
            return stream

        self.blob.exists.side_effect = lambda: False if not store else True
        self.container.upload_blob.side_effect = upload
        self.container.download_blob.side_effect = download
        self.container.list_blobs.side_effect = lambda **kw: [
            SimpleNamespace(name=n) for n in store
        ]
        self.container.delete_blob.side_effect = lambda name, **kw: store.pop(name, None)
        check_storage(self.st)
        self.assertEqual(store, {})


class ServingTests(AzureBase):
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
        self.blob.exists.return_value = False
        r = Client().get(f"/media/{asset.public_id}", HTTP_HOST="acme.test")
        self.assertEqual(r.status_code, 404)


_EMU_KEYS = (
    "OSDS_TEST_AZURE_ENDPOINT", "OSDS_TEST_AZURE_ACCOUNT", "OSDS_TEST_AZURE_KEY",
    "OSDS_TEST_AZURE_CONTAINER",
)
_EMU = {k: os.environ.get(k) for k in _EMU_KEYS}


class EmulatorTests(AzureBase):
    """Real azure-storage-blob against a Blob-compatible endpoint (Azurite).

    Skipped unless ``OSDS_TEST_AZURE_*`` are all set. The endpoint is a full
    blob service URL (for Azurite, ``http://127.0.0.1:10000/<account>``). Never
    run in CI so far: no emulator image has been verified for this project.
    """

    def setUp(self):
        super().setUp()
        if not all(_EMU.values()):
            self.skipTest("OSDS_TEST_AZURE_* not set; no emulator is configured")
        from azure.storage.blob import BlobServiceClient

        self.configure(
            {"bucket": _EMU["OSDS_TEST_AZURE_CONTAINER"], "account_name": _EMU["OSDS_TEST_AZURE_ACCOUNT"]},
            secrets=False,
        )
        set_secret("storage_account_key", _EMU["OSDS_TEST_AZURE_KEY"], tenant=self.tenant)
        self.st = get_tenant_storage(self.tenant)
        # The wizard has no endpoint field, so point the client at the emulator.
        self.st._service_client = BlobServiceClient(
            _EMU["OSDS_TEST_AZURE_ENDPOINT"],
            credential={
                "account_name": _EMU["OSDS_TEST_AZURE_ACCOUNT"],
                "account_key": _EMU["OSDS_TEST_AZURE_KEY"],
            },
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
