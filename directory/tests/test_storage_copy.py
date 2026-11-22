"""directory.storage_copy -- copy a tenant's stored files to another backend.

The target is a recording in-memory backend swapped in at the SDK import seam
(the same seam ``test_storage`` uses), so no cloud account or SDK is needed. The
source is the local backend under a temp media root.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
from unittest import mock

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.files.storage import Storage
from django.test import TransactionTestCase, override_settings

from audit.models import CommandLog
from directory import storage as storage_mod
from directory import storage_copy
from directory.models import (
    ImportBatch, Listing, ListingType, MediaAsset, StorageCopy, StorageCopyFile,
)
from directory.storage import clear_storage_cache, get_tenant_storage, tenant_storage_backend
from tenants.models import Operator, Tenant
from tenants.secrets import has_secret
from tenants.storage_forms import StorageForm, storage_backend_locked

TARGET_CFG = {"backend": "s3", "bucket": "dest", "region": "r1", "endpoint": ""}
TARGET_SECRETS = {"access_key": "AK-TARGET", "secret_key": "SK-TARGET"}


class MemoryBackend(Storage):
    """A shared in-memory object store that counts writes and can be told to
    fail. Every instance built for one bucket shares its objects."""

    stores: "dict[str, dict[str, bytes]]" = {}
    writes: "list[str]" = []
    fail_keys: "set[str]" = set()
    corrupt_keys: "set[str]" = set()
    kwargs_seen: "list[dict]" = []

    def __init__(self, **kwargs):
        MemoryBackend.kwargs_seen.append(kwargs)
        self.prefix = kwargs.get("location", "")
        self.objects = MemoryBackend.stores.setdefault(kwargs.get("bucket_name", ""), {})

    def _k(self, name):
        return f"{self.prefix}{name}"

    def _save(self, name, content):
        key = self._k(name)
        if any(key.endswith(f) for f in MemoryBackend.fail_keys):
            raise OSError("simulated write failure")
        data = content.read()
        if any(key.endswith(f) for f in MemoryBackend.corrupt_keys):
            data = data[:-1] + b"?"
        if ".osds-probe/" not in key:
            MemoryBackend.writes.append(key)
        self.objects[key] = data
        return name

    def get_available_name(self, name, max_length=None):
        return name  # a real overwrite-safe backend would rename; tests assert via exists()

    def _open(self, name, mode="rb"):
        if self._k(name) not in self.objects:
            raise FileNotFoundError(name)
        return ContentFile(self.objects[self._k(name)], name=name)

    def exists(self, name):
        return self._k(name) in self.objects

    def size(self, name):
        return len(self.objects[self._k(name)])

    def delete(self, name):
        self.objects.pop(self._k(name), None)

    def listdir(self, path):
        base = self._k(path) + "/"
        files = [k[len(base):] for k in self.objects if k.startswith(base) and "/" not in k[len(base):]]
        return [], files


def _fake_import(path, backend):
    return MemoryBackend


class _Base(TransactionTestCase):
    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="osds-copy-test-")
        self.addCleanup(shutil.rmtree, self._root, ignore_errors=True)
        ctx = override_settings(OSDS_MEDIA_ROOT=self._root)
        ctx.enable()
        self.addCleanup(ctx.disable)
        clear_storage_cache()
        self.addCleanup(clear_storage_cache)
        MemoryBackend.stores = {}
        MemoryBackend.writes = []
        MemoryBackend.fail_keys = set()
        MemoryBackend.corrupt_keys = set()
        MemoryBackend.kwargs_seen = []
        patch = mock.patch.object(storage_mod, "_import_class", _fake_import)
        patch.start()
        self.addCleanup(patch.stop)

        self.tenant = Tenant.objects.create(slug="acme", name="Acme")
        self.other = Tenant.objects.create(slug="other", name="Other")
        self.operator = Operator.objects.create_user("op@example.test", "pw", is_superadmin=True)
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )
        self.listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="acme-co", name="Acme Co",
            visibility="published",
        )
        self.source = get_tenant_storage(self.tenant)
        self.files = {
            "media/a.png": b"\x89PNG-a" * 50,
            "media/b.png": b"\x89PNG-b" * 80,
            "imports/one.csv": b"name\nAcme\n",
        }
        self.assets = []
        for key in ("media/a.png", "media/b.png"):
            self.source.save(key, ContentFile(self.files[key]))
            self.assets.append(MediaAsset.all_tenants.create(
                tenant=self.tenant, listing=self.listing, role="gallery",
                status="ready", storage_key=key, byte_size=len(self.files[key]),
            ))
        self.source.save("imports/one.csv", ContentFile(self.files["imports/one.csv"]))
        self.batch = ImportBatch.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, stored_path="imports/one.csv",
        )

    def target_objects(self, bucket="dest"):
        prefix = f"{self.tenant.public_id}/"
        return {k[len(prefix):]: v for k, v in MemoryBackend.stores.get(bucket, {}).items()}

    def run_copy(self, **kw):
        return storage_copy.run_copy(
            self.tenant, TARGET_CFG, TARGET_SECRETS, operator=self.operator, **kw,
        )


class CopyTests(_Base):
    def test_copies_every_key_with_identical_content_and_switches(self):
        result = self.run_copy()
        self.assertTrue(result.switched)
        self.assertEqual(result.copied, 3)
        self.assertEqual(self.target_objects(), self.files)
        self.tenant.refresh_from_db()
        self.assertEqual(tenant_storage_backend(self.tenant), "s3")
        self.assertEqual(self.tenant.settings["storage"]["bucket"], "dest")
        self.assertTrue(has_secret("storage_access_key", tenant=self.tenant))

    def test_source_files_still_exist_after_success(self):
        self.run_copy()
        local = storage_mod.build_storage(self.tenant.public_id, {"backend": "local"}, {})
        for key, body in self.files.items():
            self.assertTrue(local.exists(key))
            with local.open(key, "rb") as fh:
                self.assertEqual(fh.read(), body)

    def test_other_tenants_files_are_not_copied(self):
        foreign = storage_mod.build_storage(self.other.public_id, {"backend": "local"}, {})
        foreign.save("media/x.png", ContentFile(b"x"))
        self.run_copy()
        self.assertNotIn("media/x.png", self.target_objects())
        self.assertEqual(len(MemoryBackend.stores["dest"]), 3)

    def test_failing_file_keeps_old_backend_and_reports_it(self):
        MemoryBackend.fail_keys = {"media/b.png"}
        result = self.run_copy()
        self.assertFalse(result.switched)
        self.assertEqual([f.key for f in result.failures], ["media/b.png"])
        self.assertIn("simulated write failure", result.failures[0].error)
        self.tenant.refresh_from_db()
        self.assertEqual(tenant_storage_backend(self.tenant), "local")
        self.assertFalse(has_secret("storage_access_key", tenant=self.tenant))
        self.assertEqual(StorageCopy.all_tenants.get(tenant=self.tenant).status, "in_progress")

    def test_unverified_copy_is_a_failure_and_never_switches(self):
        MemoryBackend.corrupt_keys = {"imports/one.csv"}
        result = self.run_copy()
        self.assertFalse(result.switched)
        failed = StorageCopyFile.all_tenants.get(key="imports/one.csv")
        self.assertEqual(failed.status, "failed")
        self.assertIn("verif", failed.error)
        self.tenant.refresh_from_db()
        self.assertEqual(tenant_storage_backend(self.tenant), "local")

    def test_missing_source_file_is_a_failure(self):
        self.source.delete("media/a.png")
        result = self.run_copy()
        self.assertFalse(result.switched)
        self.assertEqual([f.key for f in result.failures], ["media/a.png"])

    def test_resume_does_not_recopy_completed_keys(self):
        MemoryBackend.fail_keys = {"media/b.png"}
        self.run_copy()
        first_writes = [w for w in MemoryBackend.writes]
        self.assertEqual(len(first_writes), 2)
        MemoryBackend.fail_keys = set()
        MemoryBackend.writes = []
        result = self.run_copy()
        self.assertTrue(result.switched)
        self.assertEqual(MemoryBackend.writes, [f"{self.tenant.public_id}/media/b.png"])
        self.assertEqual(StorageCopy.all_tenants.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(self.target_objects(), self.files)

    def test_interrupted_run_resumes(self):
        calls = {"n": 0}
        real = storage_copy._copy_one

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            return real(*a, **kw)

        with mock.patch.object(storage_copy, "_copy_one", flaky):
            with self.assertRaises(KeyboardInterrupt):
                self.run_copy()
        MemoryBackend.writes = []
        result = self.run_copy()
        self.assertTrue(result.switched)
        self.assertEqual(len(MemoryBackend.writes), 2)
        self.assertEqual(self.target_objects(), self.files)

    def test_existing_identical_key_on_target_is_reported_not_rewritten(self):
        target = storage_mod.build_storage(self.tenant.public_id, TARGET_CFG, TARGET_SECRETS)
        target.save("media/a.png", ContentFile(self.files["media/a.png"]))
        MemoryBackend.writes = []
        result = self.run_copy()
        self.assertTrue(result.switched)
        self.assertEqual(result.already_present, ["media/a.png"])
        self.assertNotIn(f"{self.tenant.public_id}/media/a.png", MemoryBackend.writes)

    def test_existing_different_key_on_target_is_never_overwritten(self):
        target = storage_mod.build_storage(self.tenant.public_id, TARGET_CFG, TARGET_SECRETS)
        target.save("media/a.png", ContentFile(b"someone else's bytes"))
        result = self.run_copy()
        self.assertFalse(result.switched)
        self.assertEqual([f.key for f in result.failures], ["media/a.png"])
        self.assertIn("differ", result.failures[0].error)
        self.assertEqual(self.target_objects()["media/a.png"], b"someone else's bytes")
        self.tenant.refresh_from_db()
        self.assertEqual(tenant_storage_backend(self.tenant), "local")

    def test_files_added_after_the_first_pass_are_copied_before_switching(self):
        real = storage_copy._copy_one
        added = {"done": False}

        def add_then_copy(*a, **kw):
            if not added["done"]:
                added["done"] = True
                self.source.save("media/late.png", ContentFile(b"late"))
                MediaAsset.all_tenants.create(
                    tenant=self.tenant, listing=self.listing, role="gallery",
                    status="ready", storage_key="media/late.png",
                )
            return real(*a, **kw)

        with mock.patch.object(storage_copy, "_copy_one", add_then_copy):
            result = self.run_copy()
        self.assertTrue(result.switched)
        self.assertEqual(self.target_objects()["media/late.png"], b"late")

    def test_no_files_switches_straight_away(self):
        MediaAsset.all_tenants.all().delete()
        ImportBatch.all_tenants.all().delete()
        result = self.run_copy()
        self.assertTrue(result.switched)
        self.assertEqual(result.copied, 0)

    def test_unreachable_target_changes_nothing(self):
        with mock.patch.object(storage_mod, "check_storage", side_effect=storage_mod.StorageConfigError("down")):
            with self.assertRaises(storage_mod.StorageConfigError):
                self.run_copy()
        self.assertEqual(MemoryBackend.writes, [])
        self.assertEqual(StorageCopy.all_tenants.count(), 0)

    def test_copying_to_the_current_backend_is_refused(self):
        with self.assertRaises(storage_copy.CopyRefused):
            storage_copy.run_copy(
                self.tenant, {"backend": "local"}, {}, operator=self.operator,
            )

    def test_unfinished_copy_to_a_different_target_is_refused(self):
        MemoryBackend.fail_keys = {"media/b.png"}
        self.run_copy()
        with self.assertRaises(storage_copy.CopyRefused):
            storage_copy.run_copy(
                self.tenant, {**TARGET_CFG, "bucket": "elsewhere"}, TARGET_SECRETS,
                operator=self.operator,
            )

    def test_credentials_are_not_stored_in_copy_rows_or_command_log(self):
        MemoryBackend.fail_keys = {"media/b.png"}
        self.run_copy()
        blob = json.dumps([
            list(StorageCopy.all_tenants.values()),
            list(StorageCopyFile.all_tenants.values()),
        ], default=str)
        for secret in TARGET_SECRETS.values():
            self.assertNotIn(secret, blob)
        MemoryBackend.fail_keys = set()
        self.run_copy()
        self.assertNotIn("AK-TARGET", json.dumps(list(CommandLog.objects.values()), default=str))


class DryRunTests(_Base):
    def test_dry_run_reports_count_and_size_and_changes_nothing(self):
        plan = storage_copy.plan_copy(self.tenant)
        self.assertEqual(plan.file_count, 3)
        self.assertEqual(plan.total_bytes, sum(len(v) for v in self.files.values()))
        self.assertEqual(sorted(p.key for p in plan.files), sorted(self.files))
        self.assertEqual(plan.missing, [])
        self.assertEqual(StorageCopy.all_tenants.count(), 0)
        self.assertEqual(MemoryBackend.stores, {})
        self.tenant.refresh_from_db()
        self.assertEqual(tenant_storage_backend(self.tenant), "local")

    def test_dry_run_lists_missing_source_files(self):
        self.source.delete("media/a.png")
        plan = storage_copy.plan_copy(self.tenant)
        self.assertEqual(plan.missing, ["media/a.png"])
        self.assertEqual(plan.file_count, 2)


class AlphaBlockTests(_Base):
    def test_blocked_before_a_copy(self):
        self.assertTrue(storage_backend_locked(self.tenant))
        self.assertTrue(storage_backend_locked(self.tenant, TARGET_CFG))

    def test_partial_copy_does_not_lift_the_block(self):
        MemoryBackend.fail_keys = {"media/b.png"}
        self.run_copy()
        self.assertTrue(storage_backend_locked(self.tenant, TARGET_CFG))

    def test_lifted_for_the_verified_target_only(self):
        self.run_copy()
        self.assertFalse(storage_backend_locked(self.tenant, TARGET_CFG))
        self.assertTrue(storage_backend_locked(self.tenant, {**TARGET_CFG, "bucket": "other"}))
        self.assertTrue(storage_backend_locked(self.tenant, {"backend": "gcp", "bucket": "g"}))

    def test_other_tenants_stay_blocked(self):
        self.run_copy()
        other_lt = ListingType.all_tenants.create(
            tenant=self.other, key="b", label_singular="B", label_plural="Bs", path_segment="b",
        )
        ImportBatch.all_tenants.create(tenant=self.other, listing_type=other_lt, stored_path="imports/x.csv")
        self.assertTrue(storage_backend_locked(self.other, TARGET_CFG))

    def test_a_file_added_after_the_copy_re_locks(self):
        self.run_copy()
        MediaAsset.all_tenants.create(
            tenant=self.tenant, listing=self.listing, role="logo", status="ready",
            storage_key="media/new.png",
        )
        self.assertTrue(storage_backend_locked(self.tenant, TARGET_CFG))

    def test_form_allows_a_verified_target_and_refuses_another(self):
        self.run_copy()
        # Put the tenant back on local, as if the setting had been reverted, so
        # the form sees a backend change to the verified target.
        self.tenant.settings = {"storage": {"backend": "local"}}
        self.tenant.save()
        base = {
            "backend": "s3", "s3_bucket": "dest", "s3_region": "r1",
            "s3_access_key": "AK-TARGET", "s3_secret_key": "SK-TARGET",
        }
        ok = StorageForm(base, tenant=self.tenant)
        self.assertTrue(ok.is_valid(), ok.errors)
        bad = StorageForm({**base, "s3_bucket": "not-copied"}, tenant=self.tenant)
        self.assertFalse(bad.is_valid())
        self.assertIn("backend", bad.errors)


class CommandTests(_Base):
    def _target_file(self):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(lambda: __import__("os").unlink(fh.name))
        json.dump({**TARGET_CFG, "secrets": TARGET_SECRETS}, fh)
        fh.close()
        return fh.name

    def test_dry_run_command_prints_count_and_size(self):
        out = io.StringIO()
        call_command("copy_storage", "--tenant", "acme", "--dry-run", stdout=out)
        text = out.getvalue()
        self.assertIn("3 file(s)", text)
        self.assertIn(str(sum(len(v) for v in self.files.values())), text)
        self.assertEqual(StorageCopy.all_tenants.count(), 0)

    def test_command_copies_and_switches(self):
        out = io.StringIO()
        call_command(
            "copy_storage", "--tenant", "acme", "--target-file", self._target_file(),
            "--operator", "op@example.test", stdout=out,
        )
        self.assertIn("switched", out.getvalue())
        self.assertNotIn("AK-TARGET", out.getvalue())
        self.tenant.refresh_from_db()
        self.assertEqual(tenant_storage_backend(self.tenant), "s3")

    def test_command_fails_non_zero_on_a_failed_file(self):
        MemoryBackend.fail_keys = {"media/b.png"}
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "copy_storage", "--tenant", "acme", "--target-file", self._target_file(),
                "--operator", "op@example.test", stdout=io.StringIO(), stderr=io.StringIO(),
            )
        self.assertIn("media/b.png", str(ctx.exception))
        self.assertNotIn("AK-TARGET", str(ctx.exception))

    def test_command_requires_operator_to_switch(self):
        with self.assertRaises(CommandError):
            call_command(
                "copy_storage", "--tenant", "acme", "--target-file", self._target_file(),
                stdout=io.StringIO(),
            )
