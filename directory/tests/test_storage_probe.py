"""The storage probe, the shared storage form (wizard and tenant-admin page),
the backend-switch guard, the misconfiguration banner and the legacy-key
migration. No cloud SDK or account is needed: backends are fakes at the import
seam and the probe is mocked where a form test only needs its outcome."""

from __future__ import annotations

import functools
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse as _reverse
from django.utils import timezone

from directory import storage as storage_mod
from directory.models import Listing, ListingType, MediaAsset
from directory.storage import StorageConfigError, check_storage, get_tenant_storage
from directory.tests.test_storage import SA_JSON, FakeBackend, _Base, _Mixin
from tenants.models import InstallSetup, Operator, StaffMembership
from tenants.storage_forms import StorageForm

reverse = functools.partial(_reverse, urlconf="osds.urls_tenant")

class CheckStorageTests(_Base):
    def test_a_working_backend_passes_and_leaves_nothing(self):
        fake = FakeBackend()
        check_storage(fake)
        self.assertEqual(fake.objects, {})

    def test_local_passes(self):
        check_storage(get_tenant_storage(self.tenant))

    def test_a_failure_names_the_step_and_the_provider_error(self):
        class Denied(FakeBackend):
            def _save(self, name, content):
                raise PermissionError("AccessDenied: nope")

        with self.assertRaises(StorageConfigError) as cm:
            check_storage(Denied())
        self.assertIn("write", str(cm.exception))
        self.assertIn("AccessDenied", str(cm.exception))

    def test_a_failure_after_write_still_cleans_up(self):
        class BadList(FakeBackend):
            def listdir(self, path):
                raise OSError("boom")

        fake = BadList()
        with self.assertRaises(StorageConfigError):
            check_storage(fake)
        self.assertEqual(fake.objects, {})


class CommandTests(_Base):
    def test_check_storage_command(self):
        out = StringIO()
        call_command("check_storage", "--tenant", "acme", stdout=out)
        self.assertIn("acme: ok", out.getvalue())

    def test_check_storage_command_fails_nonzero_on_a_bad_config(self):
        self.configure({"backend": "s3"})
        with self.assertRaises(CommandError):
            call_command("check_storage", "--tenant", "acme", stderr=StringIO(), stdout=StringIO())


class FormTests(_Base):
    def form(self, data, tenant=None):
        return StorageForm(data, tenant=tenant or self.tenant)

    def good(self):
        patcher = mock.patch("tenants.storage_forms.check_storage")
        self.probe = patcher.start()
        self.addCleanup(patcher.stop)

    def test_local_needs_nothing(self):
        self.assertTrue(self.form({"backend": "local"}).is_valid())

    def test_each_cloud_backend_requires_its_own_fields_only(self):
        for backend, fields in {
            "s3": {"s3_bucket", "s3_region", "s3_access_key", "s3_secret_key"},
            "azure": {"azure_container", "azure_account_name", "azure_account_key"},
            "gcp": {"gcp_bucket", "gcp_service_account_json"},
        }.items():
            with self.subTest(backend):
                f = self.form({"backend": backend})
                self.assertFalse(f.is_valid())
                self.assertEqual(set(f.errors), fields)

    def test_bad_service_account_json_is_a_field_error(self):
        f = self.form({"backend": "gcp", "gcp_bucket": "b", "gcp_service_account_json": "{x"})
        self.assertFalse(f.is_valid())
        self.assertIn("gcp_service_account_json", f.errors)

    def test_a_valid_cloud_form_probes_with_the_entered_credentials(self):
        self.good()
        with mock.patch("tenants.storage_forms.build_storage") as build:
            f = self.form({
                "backend": "azure", "azure_container": "c", "azure_account_name": "a",
                "azure_account_key": "KEY",
            })
            self.assertTrue(f.is_valid(), f.errors)
        build.assert_called_once()
        self.assertEqual(build.call_args.args[2], {"account_key": "KEY"})
        self.probe.assert_called_once()
        self.assertEqual(f.config(), {"backend": "azure", "bucket": "c", "account_name": "a"})
        self.assertEqual(f.new_secrets(), {"account_key": "KEY"})

    def test_a_failed_probe_refuses_the_form_and_shows_the_provider_error(self):
        with mock.patch("tenants.storage_forms.build_storage"), \
                mock.patch("tenants.storage_forms.check_storage",
                           side_effect=StorageConfigError("storage check failed at write: AccessDenied")):
            f = self.form({
                "backend": "azure", "azure_container": "c", "azure_account_name": "a",
                "azure_account_key": "KEY",
            })
            self.assertFalse(f.is_valid())
        text = " ".join(f.non_field_errors())
        self.assertIn("AccessDenied", text)
        self.assertIn("local", text.lower())

    def test_a_blank_secret_keeps_the_stored_one(self):
        self.good()
        self.configure({"backend": "azure", "bucket": "c", "account_name": "a"}, account_key="STORED")
        with mock.patch("tenants.storage_forms.build_storage") as build:
            f = self.form({"backend": "azure", "azure_container": "c2", "azure_account_name": "a"})
            self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(build.call_args.args[2], {"account_key": "STORED"})
        self.assertEqual(f.new_secrets(), {})

    def test_the_legacy_access_key_is_carried_into_a_secret(self):
        self.good()
        self.configure({"backend": "s3", "bucket": "b", "region": "r", "access_key": "LEGACY"}, secret_key="SK")
        with mock.patch("tenants.storage_forms.build_storage"):
            f = self.form({"backend": "s3", "s3_bucket": "b", "s3_region": "r"})
            self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.new_secrets(), {"access_key": "LEGACY"})
        self.assertNotIn("access_key", f.config())

    def test_secret_values_are_never_rendered_back(self):
        self.good()
        self.configure({"backend": "azure", "bucket": "c", "account_name": "a"}, account_key="STORED-KEY")
        f = self.form({"backend": "gcp", "gcp_bucket": "b", "gcp_service_account_json": SA_JSON.replace("PK", "PRIVATE-X") + "{"})
        self.assertFalse(f.is_valid())
        html = f.as_p()
        for secret in ("STORED-KEY", "PRIVATE-X"):
            self.assertNotIn(secret, html)
        self.assertEqual(StorageForm(tenant=self.tenant).stored_secret_names(), ["azure_account_key"])

    def test_all_fieldsets_render_without_javascript(self):
        html = StorageForm(tenant=self.tenant).as_p()
        for name in ("s3_bucket", "azure_container", "gcp_bucket"):
            self.assertIn(name, html)

    def test_the_backend_cannot_change_once_media_or_imports_exist(self):
        lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="b", label_singular="B", label_plural="Bs", path_segment="bs",
        )
        listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug="l", name="L", visibility="published",
        )
        self.good()
        f = self.form({"backend": "gcp", "gcp_bucket": "b", "gcp_service_account_json": SA_JSON})
        self.assertTrue(f.is_valid(), f.errors)  # no rows yet
        MediaAsset.all_tenants.create(
            tenant=self.tenant, listing=listing, role="gallery", status="ready", storage_key="k",
            content_type="image/png", byte_size=1, width=1, height=1, checksum_sha256="x" * 64,
        )
        f = self.form({"backend": "gcp", "gcp_bucket": "b", "gcp_service_account_json": SA_JSON})
        self.assertFalse(f.is_valid())
        self.assertIn("backend", f.errors)
        self.assertTrue(self.form({"backend": "local"}).is_valid())  # unchanged backend is fine


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test",
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class StoragePageTests(_Mixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant.primary_domain = "acme.test"
        self.tenant.save()
        self.admin = Operator.objects.create_user(email="admin@acme.test", password="pw")
        StaffMembership.objects.create(
            operator=self.admin, tenant=self.tenant, role=StaffMembership.Role.ADMIN,
            status=StaffMembership.Status.ACTIVE,
        )
        self.client = Client()
        self.client.force_login(self.admin)
        self.url = reverse("directory_admin:storage-settings")

    def test_a_misconfigured_tenant_sees_the_banner(self):
        self.configure({"backend": "s3", "bucket": "b"})
        r = self.client.get(self.url, HTTP_HOST="acme.test")
        self.assertContains(r, "Storage is misconfigured")

    def test_a_healthy_tenant_sees_no_banner(self):
        r = self.client.get(self.url, HTTP_HOST="acme.test")
        self.assertNotContains(r, "Storage is misconfigured")

    def test_saving_a_cloud_backend_stores_secrets_not_cleartext(self):
        with mock.patch("tenants.storage_forms.check_storage"), \
                mock.patch("tenants.storage_forms.build_storage"):
            r = self.client.post(self.url, {
                "backend": "s3", "s3_bucket": "b", "s3_region": "r",
                "s3_access_key": "AK", "s3_secret_key": "SK",
            }, HTTP_HOST="acme.test")
        self.assertEqual(r.status_code, 302)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.settings["storage"], {"backend": "s3", "bucket": "b", "region": "r", "endpoint": ""})
        self.assertEqual(storage_mod.load_secrets(self.tenant, "s3"), {"access_key": "AK", "secret_key": "SK"})

    def test_a_failed_probe_saves_nothing(self):
        with mock.patch("tenants.storage_forms.build_storage"), \
                mock.patch("tenants.storage_forms.check_storage", side_effect=StorageConfigError("denied")):
            r = self.client.post(self.url, {
                "backend": "s3", "s3_bucket": "b", "s3_region": "r",
                "s3_access_key": "AK", "s3_secret_key": "SK",
            }, HTTP_HOST="acme.test")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "SK")
        self.tenant.refresh_from_db()
        self.assertNotIn("storage", self.tenant.settings)


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class LegacyMigrationTests(_Base):
    def test_the_cleartext_access_key_moves_into_a_secret(self):
        import importlib

        from django.apps import apps

        mig = importlib.import_module("tenants.migrations.0005_storage_access_key_to_secret")
        self.configure({"backend": "s3", "bucket": "b", "region": "r", "access_key": "LEGACY"})
        mig.forwards(apps, None)
        self.tenant.refresh_from_db()
        self.assertNotIn("access_key", self.tenant.settings["storage"])
        self.assertEqual(storage_mod.load_secrets(self.tenant, "s3"), {"access_key": "LEGACY"})
        mig.forwards(apps, None)  # idempotent
        self.assertEqual(storage_mod.load_secrets(self.tenant, "s3"), {"access_key": "LEGACY"})


class ProbeFailsCleanlyTests(_Base):
    def test_a_missing_sdk_is_a_config_error_not_an_import_error(self):
        patch = mock.patch.object(storage_mod, "_import_class", self._real_import_class)
        patch.start()
        self.addCleanup(patch.stop)
        self.configure({"backend": "s3", "bucket": "b", "region": "r"}, access_key="a", secret_key="s")
        with mock.patch("importlib.import_module", side_effect=ImportError("no boto3")):
            with self.assertRaises(StorageConfigError):
                check_storage(get_tenant_storage(self.tenant))

    def test_missing_credentials_are_a_config_error(self):
        self.configure({"backend": "azure", "bucket": "c", "account_name": "a"})
        with self.assertRaises(StorageConfigError):
            check_storage(get_tenant_storage(self.tenant))

    def test_the_command_reports_every_failing_tenant_without_a_traceback(self):
        self.configure({"backend": "gcp"})
        err = StringIO()
        with self.assertRaises(CommandError):
            call_command("check_storage", stderr=err, stdout=StringIO())
        self.assertIn("acme: FAILED (gcp)", err.getvalue())

    def test_the_command_never_prints_a_credential(self):
        self.configure({"backend": "s3", "bucket": "b", "region": "r"}, access_key="AK-VALUE", secret_key="SK-VALUE")

        class Denied(FakeBackend):
            def _save(self, name, content):
                raise PermissionError("AccessDenied")

        with mock.patch.object(storage_mod, "_import_class", lambda p, b: Denied):
            err = StringIO()
            with self.assertRaises(CommandError):
                call_command("check_storage", "--tenant", "acme", stderr=err, stdout=StringIO())
        self.assertNotIn("SK-VALUE", err.getvalue())
        self.assertNotIn("AK-VALUE", err.getvalue())


class LegacyFallbackTests(_Base):
    def test_the_legacy_key_still_builds_a_backend_before_the_migration_runs(self):
        self.configure({"backend": "s3", "bucket": "b", "region": "r", "access_key": "LEGACY"}, secret_key="SK")
        get_tenant_storage(self.tenant)
        self.assertEqual(FakeBackend.instances[-1].kwargs["access_key"], "LEGACY")


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test",
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class BannerAndGuardTests(_Mixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant.primary_domain = "acme.test"
        self.tenant.save()
        self.admin = Operator.objects.create_user(email="admin2@acme.test", password="pw")
        StaffMembership.objects.create(
            operator=self.admin, tenant=self.tenant, role=StaffMembership.Role.ADMIN,
            status=StaffMembership.Status.ACTIVE,
        )
        self.client = Client()
        self.client.force_login(self.admin)

    def test_the_banner_shows_on_other_admin_pages_with_a_link_to_the_page(self):
        self.configure({"backend": "s3", "bucket": "b"})
        r = self.client.get(reverse("directory_admin:index"), HTTP_HOST="acme.test")
        self.assertContains(r, "Storage is misconfigured")
        self.assertContains(r, reverse("directory_admin:storage-settings"))

    def test_a_healthy_tenant_sees_no_banner_on_the_index(self):
        r = self.client.get(reverse("directory_admin:index"), HTTP_HOST="acme.test")
        self.assertNotContains(r, "Storage is misconfigured")

    def test_the_settings_page_refuses_a_backend_switch_when_media_exists(self):
        lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="b", label_singular="B", label_plural="Bs", path_segment="bs",
        )
        listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug="l", name="L", visibility="published",
        )
        MediaAsset.all_tenants.create(
            tenant=self.tenant, listing=listing, role="gallery", status="ready", storage_key="k",
            content_type="image/png", byte_size=1, width=1, height=1, checksum_sha256="x" * 64,
        )
        with mock.patch("tenants.storage_forms.check_storage"), \
                mock.patch("tenants.storage_forms.build_storage"):
            r = self.client.post(reverse("directory_admin:storage-settings"), {
                "backend": "gcp", "gcp_bucket": "b", "gcp_service_account_json": SA_JSON,
            }, HTTP_HOST="acme.test")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "cannot be changed")
        self.tenant.refresh_from_db()
        self.assertNotIn("storage", self.tenant.settings)

    def test_the_wizard_refuses_a_failed_probe_and_saves_nothing(self):
        from tenants.models import InstallSetup as IS

        IS.objects.all().update(completed_at=None)
        self.client.logout()
        # the wizard's own gating is covered in test_wizard; here the form is the unit
        f = StorageForm({"backend": "s3", "s3_bucket": "b", "s3_region": "r",
                         "s3_access_key": "a", "s3_secret_key": "s"}, tenant=self.tenant)
        with mock.patch("tenants.storage_forms.build_storage"), \
                mock.patch("tenants.storage_forms.check_storage", side_effect=StorageConfigError("denied")):
            self.assertFalse(f.is_valid())
        self.assertIn("Nothing was saved", " ".join(f.non_field_errors()))
