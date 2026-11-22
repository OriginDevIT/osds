"""CSV import: default-country phone normalisation and multi-value cells.

Worker-level tests (``import_once``) plus unit tests for the pure helpers.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from directory import csv_import, normalize
from directory.field_schema import SchemaError
from directory.models import Category, ImportBatch
from directory.tests.test_import_worker import _Base
from osds.tenancy import tenant_context

_BASE_MAP = {"name": "name", "slug": "slug"}


class _Mixed(_Base):
    def setUp(self):
        super().setUp()
        self.lt.fields = [
            {"key": "services", "label": "Services", "type": "multi_select",
             "required": False, "options": ["a", "b", "c"]},
        ]
        self.lt.save()
        Category.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="a", name="A",
        )
        Category.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="b", name="B",
        )

    def _go(self, body, mapping, **batch_kwargs):
        batch = self._batch(body, mapping={**_BASE_MAP, **mapping})
        for k, v in batch_kwargs.items():
            setattr(batch, k, v)
        batch.save()
        self._run()
        batch.refresh_from_db()
        return batch


PHONE = {"phone": "contact.phone_e164"}


class PhoneImportTests(_Mixed):
    def test_national_number_with_default_country_is_e164(self):
        batch = self._go(b"name,slug,phone\nA,a1,(555) 123-4567\n", PHONE,
                         phone_default_country="US")
        self.assertEqual(batch.error_count, 0)
        self.assertEqual(self._listings()["a1"].phone_e164, "+15551234567")

    def test_national_number_without_default_country_is_a_row_error(self):
        batch = self._go(b"name,slug,phone\nA,a1,(555) 123-4567\n", PHONE)
        self.assertEqual(batch.error_count, 1)
        self.assertNotIn("a1", self._listings())
        self.assertIn("country", str(batch.errors[0]))

    def test_plus_prefixed_number_is_unchanged_by_the_default(self):
        batch = self._go(b"name,slug,phone\nA,a1,+44 20 7946 0958\n", PHONE,
                         phone_default_country="US")
        self.assertEqual(batch.error_count, 0)
        self.assertEqual(self._listings()["a1"].phone_e164, "+442079460958")

    def test_too_few_and_too_many_digits_are_row_errors(self):
        body = (b"name,slug,phone\nA,a1,555-1234\n"
                b"B,b1,5551234567890123456\n")
        batch = self._go(body, PHONE, phone_default_country="US")
        self.assertEqual(batch.error_count, 2)
        self.assertEqual(self._listings(), {})

    def test_trunk_prefix_is_dropped_for_countries_that_use_one(self):
        batch = self._go(b"name,slug,phone\nA,a1,020 7946 0958\n", PHONE,
                         phone_default_country="GB")
        self.assertEqual(self._listings()["a1"].phone_e164, "+442079460958")
        self.assertEqual(batch.error_count, 0)

    def test_setting_survives_chunked_passes(self):
        body = (b"name,slug,phone\nA,a1,(555) 123-4567\nB,b1,(555) 123-4568\n")
        batch = self._batch(body, mapping={**_BASE_MAP, **PHONE})
        batch.phone_default_country = "US"
        batch.save()
        self._run(limit=1)   # first pass; the next re-reads the batch row
        self._run(limit=1)
        batch.refresh_from_db()
        self.assertEqual(batch.error_count, 0)
        self.assertEqual(self._listings()["b1"].phone_e164, "+15551234568")


class MultiValueImportTests(_Mixed):
    def test_multi_select_cell_splits_into_a_list(self):
        batch = self._go(b"name,slug,svc\nA,a1,a|b\n",
                         {"svc": "custom_fields.services"})
        self.assertEqual(batch.error_count, 0)
        self.assertEqual(self._listings()["a1"].custom_fields["services"],
                         ["a", "b"])

    def test_multi_select_value_not_in_options_is_a_row_error_naming_it(self):
        batch = self._go(b"name,slug,svc\nA,a1,a|zzz\n",
                         {"svc": "custom_fields.services"})
        self.assertEqual(batch.error_count, 1)
        self.assertIn("zzz", str(batch.errors[0]))
        self.assertNotIn("a1", self._listings())

    def test_empty_parts_are_dropped(self):
        body = b'name,slug,svc\nA,a1,a||b\nB,b1,"a|b|"\nC,c1,"|a"\n'
        batch = self._go(body, {"svc": "custom_fields.services"})
        self.assertEqual(batch.error_count, 0)
        for slug in ("a1", "b1"):
            self.assertEqual(
                self._listings()[slug].custom_fields["services"], ["a", "b"])
        self.assertEqual(self._listings()["c1"].custom_fields["services"], ["a"])

    def test_semicolon_separator_imports_categories_when_chosen(self):
        batch = self._go(b'name,slug,cat\nA,a1,a;b\n', {"cat": "categories"},
                         column_separators={"cat": ";"})
        self.assertEqual(batch.error_count, 0)
        with tenant_context(self.tenant):
            slugs = sorted(
                c.slug for c in self._listings()["a1"].categories.all())
        self.assertEqual(slugs, ["a", "b"])

    def test_semicolon_cell_with_pipe_separator_is_unknown_category(self):
        batch = self._go(b'name,slug,cat\nA,a1,a;b\n', {"cat": "categories"})
        self.assertEqual(batch.error_count, 1)
        # The cell is not split on ";", and the value resolves to the slug the
        # categories step derives from it.
        self.assertIn("unknown category: ab", str(batch.errors[0]))

    def test_multi_select_uses_its_own_separator(self):
        batch = self._go(b'name,slug,svc\nA,a1,a;b\n',
                         {"svc": "custom_fields.services"},
                         column_separators={"svc": ";"})
        self.assertEqual(batch.error_count, 0)
        self.assertEqual(self._listings()["a1"].custom_fields["services"],
                         ["a", "b"])


class _LT:
    fields = [{"key": "services", "label": "S", "type": "multi_select",
               "options": ["a", "b"]}]


class HelperTests(SimpleTestCase):
    def test_build_payload_national_phone_needs_country(self):
        with self.assertRaises(SchemaError):
            csv_import.build_payload(["(555) 123-4567"], ["p"],
                                     {"p": "contact.phone_e164"})
        out = csv_import.build_payload(
            ["(555) 123-4567"], ["p"], {"p": "contact.phone_e164"},
            phone_country="US")
        self.assertEqual(out, {"contact": {"phone_e164": "+15551234567"}})

    def test_build_payload_multi_select_list(self):
        out = csv_import.build_payload(
            [" a | b ||"], ["s"], {"s": "custom_fields.services"},
            listing_type=_LT())
        self.assertEqual(out, {"custom_fields": {"services": ["a", "b"]}})

    def test_unknown_country_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize.national_phone_to_e164("5551234567", "ZZ")

    def test_international_dialling_prefix_is_not_guessed(self):
        with self.assertRaises(ValueError):
            normalize.national_phone_to_e164("0044 20 7946 0958", "GB")

    def test_validate_separators(self):
        self.assertEqual(
            csv_import.clean_separators({"s": ";", "x": "|"},
                                        {"s": "custom_fields.services", "x": "name"}),
            {"s": ";"})
        with self.assertRaises(csv_import.MappingError):
            csv_import.clean_separators({"s": "abcdefg"},
                                        {"s": "custom_fields.services"})


# ---------------------------------------------------------------------------
# mapping page: saving the settings and the normalised preview
# ---------------------------------------------------------------------------
import shutil  # noqa: E402
import tempfile  # noqa: E402

from django.core.files.base import ContentFile  # noqa: E402
from django.test import TestCase, override_settings  # noqa: E402

from directory.storage import get_tenant_storage  # noqa: E402
from directory.tests.test_import_upload import HOST, _FAST_HASH, _Setup  # noqa: E402


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class MappingPageTests(_Setup, TestCase):
    def setUp(self):
        super().setUp()
        root = tempfile.mkdtemp(prefix="osds-preview-test-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        ctx = override_settings(OSDS_MEDIA_ROOT=root)
        ctx.enable()
        self.addCleanup(ctx.disable)
        self.lt.fields = [{"key": "services", "label": "Services",
                           "type": "multi_select", "options": ["a", "b"]}]
        self.lt.save()

    def _batch(self, body):
        key = get_tenant_storage(self.tenant).save("imports/p.csv", ContentFile(body))
        return ImportBatch.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, source="csv",
            status=ImportBatch.Status.MAPPING, stored_path=key,
            detected_headers=["name", "slug", "phone", "svc"],
            column_mapping={"name": "name", "slug": "slug",
                            "phone": "contact.phone_e164",
                            "svc": "custom_fields.services"},
        )

    def _post(self, b, **extra):
        data = {"map__0": "name", "map__1": "slug", "map__2": "contact.phone_e164",
                "map__3": "custom_fields.services", **extra}
        return self._client(self.editor).post(
            self._url("import-mapping", public_id=b.public_id), data, HTTP_HOST=HOST)

    def test_post_saves_country_and_separators(self):
        b = self._batch(b"name,slug,phone,svc\n")
        self._post(b, phone_country="gb", sep__3=";", sep__0=";")
        b.refresh_from_db()
        self.assertEqual(b.phone_default_country, "GB")
        # sep on a non-multi column is dropped
        self.assertEqual(b.column_separators, {"svc": ";"})

    def test_unsupported_country_is_refused_and_nothing_saved(self):
        b = self._batch(b"name,slug,phone,svc\n")
        self._post(b, phone_country="ZZ", sep__3=";")
        b.refresh_from_db()
        self.assertEqual(b.phone_default_country, "")
        self.assertEqual(b.column_separators, {})

    def test_preview_shows_normalised_values_and_errors(self):
        b = self._batch(
            b"name,slug,phone,svc\nA,a,(555) 123-4567,a|b\nB,b,12,a|zzz\n")
        b.phone_default_country = "US"
        b.save()
        resp = self._client(self.editor).get(
            self._url("import-detail", public_id=b.public_id), HTTP_HOST=HOST)
        self.assertContains(resp, "+15551234567")
        self.assertContains(resp, "a · b")
        self.assertContains(resp, "10-digit")
