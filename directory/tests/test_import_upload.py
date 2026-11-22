"""CSV import PR 1: the upload view, the mapping UI, the import.upload command
log, and the IMPORT_TARGETS / _apply_payload drift guard.

No worker, no row processing, no import.* events in this PR.
"""

from __future__ import annotations

import ast
import functools
import inspect
import re

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import (
    Client,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import reverse as _reverse
from django.utils import timezone

from audit.models import CommandLog, OutboxEvent
from directory import csv_import, services
from directory.models import Category, ImportBatch, ListingType
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant

reverse = functools.partial(_reverse, urlconf="osds.urls_tenant")
HOST = "acme.test"
_FAST_HASH = ["django.contrib.auth.hashers.MD5PasswordHasher"]


# ---------------------------------------------------------------------------
# IMPORT_TARGETS must not drift from what the service actually reads
# ---------------------------------------------------------------------------
def _keys_the_service_reads() -> set[str]:
    """Every string constant appearing in ``_apply_payload`` / ``_apply_upsert``
    plus the ``_LOCATION_KEYS`` tuple those functions loop over. A mappable
    target whose leaf key is not in here is a target the service will not
    consume."""
    tree = ast.parse(inspect.getsource(services))
    wanted = {"_apply_payload", "_apply_upsert"}
    keys: set[str] = set(services._LOCATION_KEYS)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    keys.add(sub.value)
    return keys


class ImportTargetsDriftTests(SimpleTestCase):
    def test_every_target_is_read_by_apply_payload(self):
        read = _keys_the_service_reads()
        for target, _label in csv_import.IMPORT_TARGETS:
            leaf = "custom_fields" if target.startswith("custom_fields.") else target.split(".")[-1]
            self.assertIn(
                leaf,
                read,
                f"IMPORT_TARGETS has {target!r} but _apply_payload/_apply_upsert "
                f"never read {leaf!r}",
            )

    def test_rejected_fields_are_not_targets(self):
        leaves = {t.split(".")[-1] for t, _ in csv_import.IMPORT_TARGETS}
        for rejected in services._REJECTED_KEYS:
            self.assertNotIn(rejected, leaves)

    def test_no_name_or_address_match_rule(self):
        # §7.1: matching is `id` or `(tenant, slug)` only.
        targets = {t for t, _ in csv_import.IMPORT_TARGETS}
        self.assertIn("id", targets)
        self.assertIn("slug", targets)
        self.assertNotIn("match_name", targets)
        self.assertNotIn("match_phone", targets)


# ---------------------------------------------------------------------------
# mapping validation
# ---------------------------------------------------------------------------
class _FakeType:
    fields = [{"key": "licence", "label": "Licence no", "type": "text"}]


class ValidateMappingTests(SimpleTestCase):
    HEADERS = ["Business Name", "URL slug", "Phone", "Notes", "Licence"]

    def _validate(self, submitted):
        return csv_import.validate_mapping(
            submitted, headers=self.HEADERS, listing_type=_FakeType()
        )

    def test_happy_path_drops_ignored_and_keeps_targets(self):
        cleaned = self._validate(
            {
                "Business Name": "name",
                "URL slug": "slug",
                "Phone": "contact.phone_e164",
                "Notes": "",
                "Licence": "custom_fields.licence",
            }
        )
        self.assertEqual(
            cleaned,
            {
                "Business Name": "name",
                "URL slug": "slug",
                "Phone": "contact.phone_e164",
                "Licence": "custom_fields.licence",
            },
        )

    def test_unknown_column_is_rejected(self):
        with self.assertRaises(csv_import.MappingError):
            self._validate({"Nope": "name", "URL slug": "slug"})

    def test_unknown_target_is_rejected(self):
        with self.assertRaises(csv_import.MappingError):
            self._validate({"Business Name": "visibility", "URL slug": "slug"})

    def test_duplicate_target_is_rejected(self):
        with self.assertRaises(csv_import.MappingError):
            self._validate({"Business Name": "name", "Notes": "name", "URL slug": "slug"})

    def test_requires_a_match_key(self):
        with self.assertRaises(csv_import.MappingError):
            self._validate({"Business Name": "name"})

    def test_slug_without_name_is_rejected(self):
        with self.assertRaises(csv_import.MappingError):
            self._validate({"URL slug": "slug", "Phone": "contact.phone_e164"})

    def test_id_alone_is_allowed(self):
        self.assertEqual(self._validate({"URL slug": "id"}), {"URL slug": "id"})

    def _validate_generate(self, submitted):
        return csv_import.validate_mapping(
            submitted,
            headers=self.HEADERS,
            listing_type=_FakeType(),
            generate_slug=True,
        )

    def test_generate_slug_accepts_name_without_id_or_slug(self):
        self.assertEqual(
            self._validate_generate({"Business Name": "name"}),
            {"Business Name": "name"},
        )

    def test_generate_slug_without_name_is_rejected(self):
        with self.assertRaises(csv_import.MappingError):
            self._validate_generate({"Phone": "contact.phone_e164"})

    def test_generate_slug_with_a_slug_column_is_rejected(self):
        with self.assertRaises(csv_import.MappingError):
            self._validate_generate({"Business Name": "name", "URL slug": "slug"})

    def test_generate_slug_with_an_id_column_is_rejected(self):
        # A derived slug on an id-matched row would rewrite the slug.
        with self.assertRaises(csv_import.MappingError):
            self._validate_generate({"Business Name": "name", "URL slug": "id"})

    def test_no_id_no_slug_no_name_no_generate_is_rejected(self):
        with self.assertRaises(csv_import.MappingError):
            self._validate({"Phone": "contact.phone_e164"})


class SuggestMappingTests(SimpleTestCase):
    def _suggest(self, headers):
        return csv_import.suggest_mapping(headers, _FakeType())

    def test_exact_path_match_is_case_insensitive(self):
        got = self._suggest(["NAME", "Email", "WEBSITE", "locality"])
        self.assertEqual(
            got,
            {
                "NAME": "name",
                "Email": "contact.email",
                "WEBSITE": "contact.website",
                "locality": "location.locality",
            },
        )

    def test_exact_label_match_is_case_insensitive(self):
        got = self._suggest(["postal code", "Region / State", "Licence No"])
        self.assertEqual(
            got,
            {
                "postal code": "location.postal_code",
                "Region / State": "location.region",
                "Licence No": "custom_fields.licence",
            },
        )

    def test_full_dotted_path_matches(self):
        self.assertEqual(
            self._suggest(["contact.email"]), {"contact.email": "contact.email"}
        )

    def test_never_suggests_id_or_slug(self):
        self.assertEqual(self._suggest(["id", "ID", "slug", "Slug"]), {})

    def test_never_suggests_for_match_key_label(self):
        self.assertEqual(
            self._suggest(["Match: existing listing ID"]), {}
        )

    def test_fuzzy_headers_get_no_suggestion(self):
        self.assertEqual(
            self._suggest(["Business Name", "E-mail", "Web site", "Phone no"]), {}
        )

    def test_near_misses_get_no_suggestion(self):
        self.assertEqual(self._suggest(["Names", "Emails"]), {})

    def test_two_columns_for_one_target_get_no_suggestion_for_either(self):
        self.assertEqual(
            self._suggest(["Name", "name", "Email"]), {"Email": "contact.email"}
        )


class ReadHeaderTests(SimpleTestCase):
    def test_reads_first_row_and_detects_delimiter(self):
        headers, delim, enc = csv_import.read_header(
            b"name;slug;phone\r\nAcme;acme;+1000\r\nBeta;beta;+2000\r\n"
        )
        self.assertEqual(headers, ["name", "slug", "phone"])
        self.assertEqual(delim, ";")
        self.assertEqual(enc, "utf-8-sig")

    def test_strips_a_utf8_bom(self):
        headers, _, enc = csv_import.read_header("﻿name,slug\nA,a\n".encode("utf-8"))
        self.assertEqual(headers, ["name", "slug"])
        self.assertEqual(enc, "utf-8-sig")

    def test_latin1_falls_back(self):
        headers, _, enc = csv_import.read_header("nom,ville\nCafé,Montréal\n".encode("cp1252"))
        self.assertEqual(headers, ["nom", "ville"])
        self.assertEqual(enc, "cp1252")


class BuildPayloadTests(SimpleTestCase):
    HEADERS = ["Name", "Slug", "City", "Phone", "Cats", "Licence", "Ignored"]
    MAPPING = {
        "Name": "name",
        "Slug": "slug",
        "City": "location.locality",
        "Phone": "contact.phone_e164",
        "Cats": "categories",
        "Licence": "custom_fields.licence",
    }

    def _build(self, cells):
        return csv_import.build_payload(cells, self.HEADERS, self.MAPPING)

    def test_flat_dotted_and_custom_targets_nest(self):
        payload = self._build(
            ["Hoffman", "hoffman", "Chicago", "+13125550100",
             "plumbers|emergency", "L-9", "whatever"]
        )
        self.assertEqual(
            payload,
            {
                "name": "Hoffman",
                "slug": "hoffman",
                "location": {"locality": "Chicago"},
                "contact": {"phone_e164": "+13125550100"},
                "categories": ["plumbers", "emergency"],
                "custom_fields": {"licence": "L-9"},
            },
        )

    def test_empty_cells_and_unmapped_columns_are_omitted(self):
        payload = self._build(["Hoffman", "hoffman", "   ", "", "", "", "x"])
        self.assertEqual(payload, {"name": "Hoffman", "slug": "hoffman"})

    def test_short_row_just_maps_what_is_present(self):
        self.assertEqual(
            self._build(["Hoffman", "hoffman"]),
            {"name": "Hoffman", "slug": "hoffman"},
        )


# ---------------------------------------------------------------------------
# the views
# ---------------------------------------------------------------------------
class _Setup:
    def setUp(self):
        super().setUp()
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain=HOST
        )
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="Business",
            label_plural="Businesses", path_segment="businesses",
            fields=[{"key": "licence", "label": "Licence no", "type": "text", "required": False}],
        )
        Category.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="plumbers", name="Plumbers"
        )
        self.editor = self._member("editor@acme.test", StaffMembership.Role.EDITOR)
        self.support = self._member("support@acme.test", StaffMembership.Role.SUPPORT)

    def _member(self, email, role):
        op = Operator.objects.create_user(email=email, password="x")
        StaffMembership.objects.create(
            operator=op, tenant=self.tenant, role=role,
            status=StaffMembership.Status.ACTIVE,
        )
        return op

    def _client(self, operator=None):
        c = Client()
        if operator:
            c.force_login(operator)
        return c

    def _url(self, name, **kw):
        return reverse(f"directory_admin:{name}", kwargs=kw)

    def _csv(self, body=b"name,slug,phone\nAcme,acme,+15550001\n", filename="in.csv"):
        return SimpleUploadedFile(filename, body, content_type="text/csv")


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class ImportUploadViewTests(_Setup, TransactionTestCase):
    # TransactionTestCase, not TestCase: _store_upload calls require_autocommit
    # and a plain TestCase wraps every test in a transaction.
    def test_support_cannot_reach_upload(self):
        resp = self._client(self.support).get(self._url("import-create"), HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_is_redirected_to_login(self):
        resp = self._client().get(self._url("import-create"), HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp["Location"])

    def test_editor_upload_creates_batch_in_mapping_state(self):
        resp = self._client(self.editor).post(
            self._url("import-create"),
            {"listing_type": self.lt.pk, "file": self._csv()},
            HTTP_HOST=HOST,
        )
        self.assertEqual(resp.status_code, 302)
        batch = ImportBatch.all_tenants.get(tenant=self.tenant)
        self.assertEqual(batch.status, ImportBatch.Status.MAPPING)
        self.assertEqual(batch.listing_type, self.lt)
        self.assertEqual(batch.detected_headers, ["name", "slug", "phone"])
        self.assertEqual(batch.delimiter, ",")
        self.assertEqual(batch.started_by, self.editor)
        self.assertTrue(batch.stored_path)

    def test_upload_writes_no_listing_no_event(self):
        self._client(self.editor).post(
            self._url("import-create"),
            {"listing_type": self.lt.pk, "file": self._csv()},
            HTTP_HOST=HOST,
        )
        self.assertEqual(OutboxEvent.all_tenants.count(), 0)

    def test_upload_logs_an_import_upload_command_pair(self):
        self._client(self.editor).post(
            self._url("import-create"),
            {"listing_type": self.lt.pk, "file": self._csv()},
            HTTP_HOST=HOST,
        )
        rows = CommandLog.objects.filter(command="import.upload")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.outcome, "applied")
        self.assertIsNotNone(row.concluded_at)
        self.assertEqual(row.tenant, self.tenant)
        self.assertEqual(row.payload["headers"], ["name", "slug", "phone"])
        self.assertEqual(row.payload["listing_type"], "business")

    def test_oversized_file_is_rejected(self):
        big = self._csv(body=b"x" * (csv_import.MAX_UPLOAD_BYTES + 1))
        resp = self._client(self.editor).post(
            self._url("import-create"),
            {"listing_type": self.lt.pk, "file": big},
            HTTP_HOST=HOST,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ImportBatch.all_tenants.count(), 0)
        self.assertEqual(CommandLog.objects.filter(command="import.upload").count(), 0)

    def test_empty_file_is_rejected(self):
        resp = self._client(self.editor).post(
            self._url("import-create"),
            {"listing_type": self.lt.pk, "file": self._csv(body=b"")},
            HTTP_HOST=HOST,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ImportBatch.all_tenants.count(), 0)


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class ImportMappingViewTests(_Setup, TestCase):
    def _batch(self, headers=("Name", "Slug", "Phone")):
        return ImportBatch.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, source="csv",
            status=ImportBatch.Status.MAPPING, detected_headers=list(headers),
            stored_path="imports/x.csv",
        )

    def test_detail_renders_a_select_per_header(self):
        b = self._batch()
        resp = self._client(self.editor).get(
            self._url("import-detail", public_id=b.public_id), HTTP_HOST=HOST
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="map__0"')
        self.assertContains(resp, 'name="map__2"')

    def _selected(self, resp, index):
        html = resp.content.decode()
        m = re.search(
            r'<select name="map__%d">(.*?)</select>' % index, html, re.S
        )
        self.assertIsNotNone(m)
        sel = re.findall(r'<option value="([^"]*)"[^>]* selected', m.group(1))
        return sel[0] if sel else ""

    def _get_detail(self, b):
        return self._client(self.editor).get(
            self._url("import-detail", public_id=b.public_id), HTTP_HOST=HOST
        )

    def test_exact_header_is_preselected_and_marked_suggested(self):
        b = self._batch(headers=("Name", "Email", "Notes"))
        resp = self._get_detail(b)
        self.assertEqual(self._selected(resp, 0), "name")
        self.assertEqual(self._selected(resp, 1), "contact.email")
        self.assertEqual(self._selected(resp, 2), "")
        self.assertContains(resp, "(suggested)", count=2)

    def test_id_and_slug_headers_stay_ignored_on_the_form(self):
        b = self._batch(headers=("id", "slug", "name"))
        resp = self._get_detail(b)
        self.assertEqual(self._selected(resp, 0), "")
        self.assertEqual(self._selected(resp, 1), "")
        self.assertEqual(self._selected(resp, 2), "name")

    def test_suggestion_is_not_saved_until_the_operator_submits(self):
        b = self._batch(headers=("Name", "Email"))
        self._get_detail(b)
        b.refresh_from_db()
        self.assertEqual(b.column_mapping, {})
        self.assertEqual(b.status, ImportBatch.Status.MAPPING)

    def test_saved_mapping_wins_over_suggestions(self):
        b = self._batch(headers=("Name", "Email", "Slug"))
        b.column_mapping = {"Slug": "slug", "Name": "description"}
        b.save(update_fields=["column_mapping"])
        resp = self._get_detail(b)
        self.assertEqual(self._selected(resp, 0), "description")
        self.assertEqual(self._selected(resp, 1), "")
        self.assertEqual(self._selected(resp, 2), "slug")
        self.assertNotContains(resp, "(suggested)")

    def test_locked_batch_shows_no_suggestions(self):
        b = self._batch(headers=("Name", "Email"))
        b.status = ImportBatch.Status.PENDING
        b.save(update_fields=["status"])
        self.assertNotContains(self._get_detail(b), "(suggested)")

    def test_operator_can_override_a_suggestion(self):
        b = self._batch(headers=("Name", "Slug", "Email"))
        self._client(self.editor).post(
            self._url("import-mapping", public_id=b.public_id),
            {"map__0": "name", "map__1": "slug", "map__2": "contact.website"},
            HTTP_HOST=HOST,
        )
        b.refresh_from_db()
        self.assertEqual(
            b.column_mapping,
            {"Name": "name", "Slug": "slug", "Email": "contact.website"},
        )

    def test_saving_a_valid_mapping_persists_it(self):
        b = self._batch()
        self._client(self.editor).post(
            self._url("import-mapping", public_id=b.public_id),
            {"map__0": "name", "map__1": "slug", "map__2": "contact.phone_e164"},
            HTTP_HOST=HOST,
        )
        b.refresh_from_db()
        self.assertEqual(
            b.column_mapping,
            {"Name": "name", "Slug": "slug", "Phone": "contact.phone_e164"},
        )

    def test_detail_labels_generate_slug_as_create_only(self):
        b = self._batch()
        resp = self._client(self.editor).get(
            self._url("import-detail", public_id=b.public_id), HTTP_HOST=HOST
        )
        self.assertContains(resp, 'name="generate_slug"')
        self.assertContains(resp, "Generate slug from name")
        self.assertContains(resp, "creates new listings only")

    def test_saving_generate_slug_persists_flag_and_mapping(self):
        b = self._batch()
        self._client(self.editor).post(
            self._url("import-mapping", public_id=b.public_id),
            {"map__0": "name", "generate_slug": "on"},
            HTTP_HOST=HOST,
        )
        b.refresh_from_db()
        self.assertTrue(b.generate_slug)
        self.assertEqual(b.column_mapping, {"Name": "name"})

    def test_generate_slug_with_slug_column_is_not_saved(self):
        b = self._batch()
        self._client(self.editor).post(
            self._url("import-mapping", public_id=b.public_id),
            {"map__0": "name", "map__1": "slug", "generate_slug": "on"},
            HTTP_HOST=HOST,
        )
        b.refresh_from_db()
        self.assertFalse(b.generate_slug)
        self.assertEqual(b.column_mapping, {})

    def test_invalid_mapping_is_not_saved(self):
        b = self._batch()
        self._client(self.editor).post(
            self._url("import-mapping", public_id=b.public_id),
            {"map__0": "visibility", "map__1": "slug"},
            HTTP_HOST=HOST,
        )
        b.refresh_from_db()
        self.assertEqual(b.column_mapping, {})

    def test_run_requires_a_saved_mapping_then_moves_to_pending(self):
        b = self._batch()
        self._client(self.editor).post(
            self._url("import-run", public_id=b.public_id), HTTP_HOST=HOST
        )
        b.refresh_from_db()
        self.assertEqual(b.status, ImportBatch.Status.MAPPING)  # no mapping yet

        b.column_mapping = {"Name": "name", "Slug": "slug"}
        b.save(update_fields=["column_mapping"])
        resp = self._client(self.editor).post(
            self._url("import-run", public_id=b.public_id),
            HTTP_HOST=HOST,
            follow=True,
        )
        b.refresh_from_db()
        self.assertEqual(b.status, ImportBatch.Status.PENDING)
        self.assertContains(resp, "Import queued")
        self.assertNotContains(resp, "No worker")

    def test_pending_detail_does_not_claim_there_is_no_worker(self):
        b = self._batch()
        b.column_mapping = {"Name": "name", "Slug": "slug"}
        b.status = ImportBatch.Status.PENDING
        b.save(update_fields=["column_mapping", "status"])
        resp = self._client(self.editor).get(
            self._url("import-detail", public_id=b.public_id), HTTP_HOST=HOST
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Queued")
        self.assertNotContains(resp, "No worker")

    def test_mapping_locked_after_run(self):
        b = self._batch()
        b.column_mapping = {"Name": "name", "Slug": "slug"}
        b.status = ImportBatch.Status.PENDING
        b.save(update_fields=["column_mapping", "status"])
        self._client(self.editor).post(
            self._url("import-mapping", public_id=b.public_id),
            {"map__0": "description", "map__1": "slug"},
            HTTP_HOST=HOST,
        )
        b.refresh_from_db()
        self.assertEqual(b.column_mapping, {"Name": "name", "Slug": "slug"})

    def test_other_tenants_batch_is_404(self):
        other = Tenant.objects.create(slug="beta", name="Beta", primary_domain="beta.test")
        b = ImportBatch.all_tenants.create(
            tenant=other, source="csv", status=ImportBatch.Status.MAPPING,
            detected_headers=["x"], stored_path="imports/y.csv",
        )
        resp = self._client(self.editor).get(
            self._url("import-detail", public_id=b.public_id), HTTP_HOST=HOST
        )
        self.assertEqual(resp.status_code, 404)
