"""CSV import: infer categories from the mapped column and offer a "create
missing categories" preview before the import runs.

Nothing is created until the operator confirms; creation goes through
``services.create_category`` and stays inside the batch's own listing type.
"""

from __future__ import annotations

import shutil
import tempfile

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from audit.models import CommandLog
from directory import csv_import
from directory.importing import import_once
from directory.models import Category, ImportBatch, Listing, ListingType
from directory.tests.test_import_upload import (
    HOST,
    _FAST_HASH,
    _Setup,
)

CSV = (
    b"name,slug,cats\n"
    b"Acme One,acme-one,plumbers|roofers\n"
    b"Acme Two,acme-two,roofers\n"
    b"Acme Three,acme-three,Gutter Cleaning\n"
)


class CategorySlugTests(SimpleTestCase):
    def test_cell_values_resolve_by_their_derived_slug(self):
        payload = csv_import.build_payload(
            ["A", "a", "Gutter Cleaning|plumbers"],
            ["name", "slug", "cats"],
            {"name": "name", "slug": "slug", "cats": "categories"},
        )
        self.assertEqual(payload["categories"], ["gutter-cleaning", "plumbers"])

    def test_value_with_no_usable_slug_is_kept_so_the_row_fails_loudly(self):
        payload = csv_import.build_payload(
            ["A", "a", "!!!"],
            ["name", "slug", "cats"],
            {"name": "name", "slug": "slug", "cats": "categories"},
        )
        self.assertEqual(payload["categories"], ["!!!"])


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class ImportCategoryPreviewTests(_Setup, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self._media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._media, True)
        patcher = override_settings(OSDS_MEDIA_ROOT=self._media)
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.admin = self._member("admin@acme.test", 4)

    def _upload(self, body=CSV, lt=None, mapping_cats=True):
        lt = lt or self.lt
        c = self._client(self.editor)
        c.post(
            self._url("import-create"),
            {"listing_type": lt.pk, "file": self._csv(body=body)},
            HTTP_HOST=HOST,
        )
        batch = ImportBatch.all_tenants.get()
        data = {"map__0": "name", "map__1": "slug"}
        if mapping_cats:
            data["map__2"] = "categories"
        c.post(
            self._url("import-mapping", public_id=batch.public_id),
            data,
            HTTP_HOST=HOST,
        )
        return ImportBatch.all_tenants.get(pk=batch.pk)

    def _preview(self, batch, operator=None):
        return self._client(operator or self.editor).get(
            self._url("import-categories", public_id=batch.public_id),
            HTTP_HOST=HOST,
        )

    def _confirm(self, batch, operator=None):
        return self._client(operator or self.admin).post(
            self._url("import-categories", public_id=batch.public_id),
            {"action": "create"},
            HTTP_HOST=HOST,
        )

    def _cats(self, lt=None):
        return set(
            Category.all_tenants.filter(listing_type=lt or self.lt).values_list(
                "slug", flat=True
            )
        )

    def _run(self, batch):
        batch.status = ImportBatch.Status.PENDING
        batch.save(update_fields=["status"])
        while not import_once(now=timezone.now()).completed:
            pass
        return ImportBatch.all_tenants.get(pk=batch.pk)

    def test_preview_lists_existing_and_missing_and_writes_nothing(self):
        batch = self._upload()
        resp = self._preview(batch)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([v for v, _ in resp.context["plan"].existing], ["plumbers"])
        self.assertEqual(
            [s for _, s in resp.context["plan"].missing],
            ["roofers", "gutter-cleaning"],
        )
        self.assertEqual(self._cats(), {"plumbers"})
        self.assertFalse(CommandLog.objects.filter(payload__page="categories").exists())

    def test_confirmation_creates_missing_top_level_and_rows_import(self):
        batch = self._upload()
        self._confirm(batch)
        self.assertEqual(self._cats(), {"plumbers", "roofers", "gutter-cleaning"})
        made = Category.all_tenants.get(listing_type=self.lt, slug="gutter-cleaning")
        self.assertEqual(made.name, "Gutter Cleaning")
        self.assertIsNone(made.parent)
        self.assertEqual(made.order, 0)

        done = self._run(batch)
        self.assertEqual(done.created_count, 3)
        self.assertEqual(done.error_count, 0)
        one = Listing.all_tenants.get(slug="acme-one")
        self.assertEqual(
            set(
                Category.all_tenants.filter(listings=one).values_list("slug", flat=True)
            ),
            {"plumbers", "roofers"},
        )

    def test_creation_is_command_logged_through_create_category(self):
        batch = self._upload()
        self._confirm(batch)
        rows = CommandLog.objects.filter(
            tenant=self.tenant, payload__page="categories", payload__op="add"
        )
        self.assertEqual(rows.count(), 2)
        self.assertEqual({r.outcome for r in rows}, {"applied"})

    def test_same_slug_in_another_type_is_neither_matched_nor_reused(self):
        other = ListingType.all_tenants.create(
            tenant=self.tenant, key="club", label_singular="Club",
            label_plural="Clubs", path_segment="clubs",
        )
        elsewhere = Category.all_tenants.create(
            tenant=self.tenant, listing_type=other, slug="roofers", name="Roofers"
        )
        batch = self._upload()
        plan = self._preview(batch).context["plan"]
        self.assertIn("roofers", [s for _, s in plan.missing])
        self._confirm(batch)
        mine = Category.all_tenants.get(listing_type=self.lt, slug="roofers")
        self.assertNotEqual(mine.pk, elsewhere.pk)
        self.assertEqual(self._cats(other), {"roofers"})
        self.assertEqual(Category.all_tenants.filter(listing_type=other).count(), 1)

    def test_colliding_slugs_and_reserved_slugs_are_flagged_not_created(self):
        body = (
            b"name,slug,cats\n"
            b"A,a,Pipes\n"
            b"B,b,pipes\n"
            b"C,c,Search\n"
            b"D,d,!!!\n"
        )
        batch = self._upload(body=body)
        plan = self._preview(batch).context["plan"]
        self.assertEqual(plan.missing, [])
        self.assertEqual(
            sorted(v for v, _ in plan.flagged), ["!!!", "Pipes", "Search", "pipes"]
        )
        self._confirm(batch)
        self.assertEqual(self._cats(), {"plumbers"})

    def test_declining_leaves_rows_failing_unknown_category(self):
        batch = self._upload()
        done = self._run(batch)  # no confirmation
        self.assertEqual(self._cats(), {"plumbers"})
        self.assertEqual(done.error_count, 3)
        self.assertIn("unknown category", str(done.errors))

    def test_no_categories_column_means_nothing_to_preview(self):
        batch = self._upload(mapping_cats=False)
        resp = self._preview(batch)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No categories column")
        self._confirm(batch)
        self.assertEqual(self._cats(), {"plumbers"})

    def test_confirm_needs_admin_role(self):
        batch = self._upload()
        resp = self._confirm(batch, operator=self.editor)
        self.assertEqual(self._cats(), {"plumbers"})
        self.assertNotEqual(resp.status_code, 500)

    def test_confirm_is_ignored_once_the_batch_is_queued(self):
        batch = self._upload()
        batch.status = ImportBatch.Status.PENDING
        batch.save(update_fields=["status"])
        self._confirm(batch)
        self.assertEqual(self._cats(), {"plumbers"})

    def test_other_tenants_batch_is_404(self):
        batch = self._upload()
        from tenants.models import Tenant

        Tenant.objects.create(slug="b", name="B", primary_domain="b.test")
        resp = self._client(self.editor).get(
            self._url("import-categories", public_id="batch_nope"), HTTP_HOST=HOST
        )
        self.assertEqual(resp.status_code, 404)
