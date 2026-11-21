"""CSV import: create the listing type from the upload, with the custom-field
schema inferred from the columns that map to no fixed target."""

from __future__ import annotations

import json
from unittest import mock

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from audit.models import CommandLog
from directory import csv_import
from directory.models import ImportBatch, ListingType, PathRedirect
from directory.storage import StorageConfigError
from directory.tests.test_import_upload import HOST, _FAST_HASH, _Setup

CSV = b"Name,Slug,Licence No,Years Trading,Notes\nAcme,acme,L1,4,hi\n"
HEADERS = ["Name", "Slug", "Licence No", "Years Trading", "Notes"]


class ProposeSchemaTests(SimpleTestCase):
    def test_one_text_field_per_unmapped_column(self):
        rows = csv_import.propose_schema(HEADERS)
        self.assertEqual(
            [(r["header"], r["key"], r["label"], r["type"]) for r in rows],
            [
                ("Licence No", "licence_no", "Licence No", "text"),
                ("Years Trading", "years_trading", "Years Trading", "text"),
                ("Notes", "notes", "Notes", "text"),
            ],
        )

    def test_id_and_slug_columns_are_not_proposed(self):
        rows = csv_import.propose_schema(["ID", "slug", "Extra"])
        self.assertEqual([r["header"] for r in rows], ["Extra"])

    def test_columns_matching_a_fixed_target_are_not_proposed(self):
        rows = csv_import.propose_schema(["name", "Email", "Region / State", "Extra"])
        self.assertEqual([r["header"] for r in rows], ["Extra"])

    def test_inference_never_picks_a_non_text_type(self):
        rows = csv_import.propose_schema(["Count", "Opened", "Active"])
        self.assertEqual({r["type"] for r in rows}, {"text"})

    def test_reserved_and_invalid_keys_are_flagged_not_renamed(self):
        rows = csv_import.propose_schema(["Status", "1st Aid", "!!!"])
        self.assertEqual([r["key"] for r in rows], ["status", "1st_aid", ""])
        errors = csv_import.check_proposal(rows)
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(errors))

    def test_duplicate_derived_keys_are_flagged(self):
        rows = csv_import.propose_schema(["Opening Hours", "opening-hours"])
        errors = csv_import.check_proposal(rows)
        self.assertTrue(any("duplicate" in e for e in errors[1]))

    def test_schema_flags_all_off(self):
        rows = csv_import.propose_schema(["Notes"])
        (field,) = csv_import.schema_from_proposal(rows)
        self.assertEqual(
            field,
            {
                "key": "notes", "label": "Notes", "type": "text",
                "required": False, "public": False, "searchable": False,
            },
        )


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class NewTypeUploadTests(_Setup, TransactionTestCase):
    def _base(self, **over):
        data = {
            "type_mode": "new",
            "new_label_singular": "Shop",
            "new_label_plural": "Shops",
            "new_key": "shop",
            "new_path_segment": "shops",
            "new_claimable": "on",
            "file": self._csv(CSV),
        }
        data.update(over)
        return data

    def _confirm(self, *, drop=(), edits=None, headers=HEADERS, **over):
        rows = csv_import.propose_schema(headers)
        data = self._base(action="confirm", proposal_headers=json.dumps(headers), **over)
        data["col"] = []
        for r in rows:
            i = r["index"]
            data["col"].append(str(i))
            if r["header"] not in drop:
                data[f"inc__{i}"] = "1"
            data[f"key__{i}"] = (edits or {}).get(r["header"], r["key"])
            data[f"label__{i}"] = r["label"]
            data[f"type__{i}"] = "text"
        return data

    def _post(self, data):
        return self._client(self.editor).post(
            self._url("import-create"), data, HTTP_HOST=HOST
        )

    def _nothing_created(self):
        self.assertEqual(ListingType.all_tenants.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(ImportBatch.all_tenants.count(), 0)
        self.assertEqual(CommandLog.objects.filter(command="import.upload").count(), 0)
        self.assertEqual(CommandLog.objects.filter(command="settings.update").count(), 0)

    def test_get_creates_nothing(self):
        resp = self._client(self.editor).get(self._url("import-create"), HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 200)
        self._nothing_created()

    def test_preview_shows_proposal_and_creates_nothing(self):
        resp = self._post(self._base())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "licence_no")
        self.assertContains(resp, "years_trading")
        self._nothing_created()

    def test_preview_proposes_key_and_path_segment_defaults(self):
        resp = self._post(self._base(new_key="", new_path_segment=""))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["form"]["new_key"].value(), "shop")
        self.assertEqual(resp.context["form"]["new_path_segment"].value(), "shop")
        self._nothing_created()

    def test_confirm_creates_type_and_batch_in_one_request(self):
        resp = self._post(self._confirm())
        self.assertEqual(resp.status_code, 302)
        new = ListingType.all_tenants.get(tenant=self.tenant, key="shop")
        self.assertEqual(new.path_segment, "shops")
        self.assertEqual(
            [(f["key"], f["type"], f["required"], f["public"], f["searchable"])
             for f in new.fields],
            [(k, "text", False, False, False)
             for k in ("licence_no", "years_trading", "notes")],
        )
        batch = ImportBatch.all_tenants.get(tenant=self.tenant)
        self.assertEqual(batch.listing_type, new)
        self.assertEqual(batch.detected_headers, HEADERS)
        self.assertEqual(CommandLog.objects.filter(command="settings.update").count(), 1)
        self.assertEqual(CommandLog.objects.filter(command="import.upload").count(), 1)

    def test_removed_column_is_absent_from_schema_and_stays_unmapped(self):
        self._post(self._confirm(drop=("Notes",)))
        new = ListingType.all_tenants.get(tenant=self.tenant, key="shop")
        self.assertNotIn("notes", [f["key"] for f in new.fields])
        batch = ImportBatch.all_tenants.get(tenant=self.tenant)
        self.assertIn("Notes", batch.detected_headers)
        self.assertEqual(batch.column_mapping, {})

    def test_second_type_records_path_redirect(self):
        self._post(self._confirm())
        redirect = PathRedirect.all_tenants.get(tenant=self.tenant, old_prefix="")
        self.assertEqual(redirect.new_prefix, "/businesses")

    def test_reserved_header_blocks_creation_until_renamed(self):
        headers = ["Name", "Slug", "Status"]
        body = b"Name,Slug,Status\nA,a,x\n"
        data = self._confirm(headers=headers, file=self._csv(body))
        resp = self._post(data)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "reserved field name")
        self._nothing_created()

        data = self._confirm(
            headers=headers, file=self._csv(body), edits={"Status": "status_text"}
        )
        self.assertEqual(self._post(data).status_code, 302)
        new = ListingType.all_tenants.get(tenant=self.tenant, key="shop")
        self.assertIn("status_text", [f["key"] for f in new.fields])

    def test_invalid_derived_key_blocks_creation(self):
        headers = ["Name", "1st Aid"]
        data = self._confirm(
            headers=headers, file=self._csv(b"Name,1st Aid\nA,b\n")
        )
        resp = self._post(data)
        self.assertEqual(resp.status_code, 200)
        self._nothing_created()

    def test_duplicate_type_key_is_a_form_error(self):
        resp = self._post(self._confirm(new_key="business"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["form"].has_error("new_key"))
        self._nothing_created()

    def test_duplicate_path_segment_is_a_form_error(self):
        resp = self._post(self._confirm(new_path_segment="businesses"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["form"].has_error("new_path_segment"))
        self._nothing_created()

    def test_key_is_never_adjusted_after_collision(self):
        self._post(self._confirm(new_key="business"))
        self.assertFalse(
            ListingType.all_tenants.filter(tenant=self.tenant, key="business_2").exists()
        )

    def test_confirm_with_a_different_file_is_refused(self):
        data = self._confirm(file=self._csv(b"Name,Other\nA,b\n"))
        resp = self._post(data)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["form"].has_error("file"))
        self._nothing_created()

    def test_storage_failure_leaves_no_type_or_batch(self):
        with mock.patch(
            "directory.admin_views.get_tenant_storage",
            side_effect=StorageConfigError("no storage"),
        ):
            resp = self._post(self._confirm())
        self.assertEqual(resp.status_code, 200)
        self._nothing_created()

    def test_failure_after_type_creation_removes_the_type(self):
        with mock.patch(
            "directory.admin_views._store_upload", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self._post(self._confirm())
        self.assertEqual(ListingType.all_tenants.filter(tenant=self.tenant).count(), 1)
        self.assertFalse(PathRedirect.all_tenants.filter(tenant=self.tenant).exists())
        self.assertEqual(ImportBatch.all_tenants.count(), 0)

    def test_existing_type_path_is_unchanged(self):
        resp = self._post(
            {"listing_type": self.lt.pk, "file": self._csv()}
        )
        self.assertEqual(resp.status_code, 302)
        batch = ImportBatch.all_tenants.get(tenant=self.tenant)
        self.assertEqual(batch.listing_type, self.lt)
        self.assertEqual(ListingType.all_tenants.filter(tenant=self.tenant).count(), 1)

    def test_existing_mode_without_a_type_is_an_error(self):
        resp = self._post({"type_mode": "existing", "file": self._csv()})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ImportBatch.all_tenants.count(), 0)
