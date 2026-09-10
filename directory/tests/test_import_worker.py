"""CSV import PR 2: the worker row loop (``directory.importing.import_once``).

TransactionTestCase throughout -- ``import_once`` calls ``upsert_listing``,
which calls ``require_autocommit``. The stored file is written to a real
per-tenant FileSystemStorage rooted at a temp dir.
"""

from __future__ import annotations

import shutil
import tempfile
from unittest import mock

from django.core.files.base import ContentFile
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from audit.models import CommandLog, OutboxEvent
from directory.importing import ImportStats, import_once
from directory.models import Category, ImportBatch, Listing, ListingType
from directory.storage import get_tenant_storage
from directory.suppression import fingerprint
from directory.models import SuppressionKey
from osds.tenancy import tenant_context
from tenants.models import Operator, Tenant

_HASH = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@override_settings(ALLOWED_HOSTS=["*"], PASSWORD_HASHERS=_HASH)
class _Base(TransactionTestCase):
    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="osds-import-test-")
        self.addCleanup(shutil.rmtree, self._root, ignore_errors=True)
        ctx = override_settings(OSDS_MEDIA_ROOT=self._root)
        ctx.enable()
        self.addCleanup(ctx.disable)
        notify = mock.patch("audit.outbox._notify_outbox")
        notify.start()
        self.addCleanup(notify.stop)

        self.tenant = Tenant.objects.create(slug="acme", name="Acme")
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
            fields=[{"key": "licence", "label": "Licence", "type": "text",
                     "required": False}],
        )
        Category.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="plumbers",
            name="Plumbers",
        )
        self.op = Operator.objects.create_user(email="e@acme.test", password="x")

    def _batch(self, body: bytes, *, mapping, started_by=None,
               delimiter=",", encoding="utf-8-sig", headers=None):
        headers = headers if headers is not None else \
            body.decode(encoding).splitlines()[0].split(delimiter)
        key = get_tenant_storage(self.tenant).save(
            "imports/in.csv", ContentFile(body)
        )
        return ImportBatch.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, source="csv",
            status=ImportBatch.Status.PENDING, stored_path=key,
            original_filename="in.csv", detected_headers=headers,
            delimiter=delimiter, encoding=encoding, has_header=True,
            column_mapping=mapping, started_by=started_by,
        )

    def _run(self, limit=50):
        return import_once(now=timezone.now(), limit=limit)

    def _events(self, etype):
        return list(
            OutboxEvent.all_tenants.filter(type=etype).order_by("id")
        )

    def _listings(self):
        return {l.slug: l for l in Listing.all_tenants.filter(tenant=self.tenant)}


MAP = {"name": "name", "slug": "slug", "city": "location.locality",
       "licence": "custom_fields.licence"}
CSV3 = b"name,slug,city,licence\nAcme,acme,Chicago,L1\nBeta,beta,,\nGamma,gamma,Peoria,L3\n"


class HappyPathTests(_Base):
    def test_full_import(self):
        batch = self._batch(CSV3, mapping=MAP, started_by=self.op)

        stats = self._run()

        self.assertEqual(stats.rows_processed, 3)
        self.assertTrue(stats.completed)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.COMPLETED)
        self.assertEqual(batch.row_count, 3)
        self.assertEqual(batch.processed_row_count, 3)
        self.assertEqual(batch.created_count, 3)
        self.assertEqual(batch.error_count, 0)
        self.assertEqual(set(self._listings()), {"acme", "beta", "gamma"})
        self.assertEqual(self._listings()["acme"].locality, "Chicago")
        self.assertEqual(
            self._listings()["acme"].custom_fields.get("licence"), "L1"
        )

    def test_started_then_completed_events(self):
        self._batch(CSV3, mapping=MAP, started_by=self.op)
        self._run()

        started = self._events("import.started")
        completed = self._events("import.completed")
        self.assertEqual(len(started), 1)
        self.assertEqual(len(completed), 1)
        self.assertEqual(started[0].data["row_count"], 3)
        self.assertEqual(started[0].data["source"], "csv")
        self.assertEqual(completed[0].data["status"], "completed")
        self.assertEqual(completed[0].data["created"], 3)
        self.assertEqual(completed[0].data["errors"], [])
        # import.* is tenant-scoped -- the envelope carries the tenant
        self.assertEqual(started[0].tenant, self.tenant)
        self.assertEqual(completed[0].tenant, self.tenant)

    def test_actor_is_staff_when_started_by_is_set(self):
        self._batch(CSV3, mapping=MAP, started_by=self.op)
        self._run()
        self.assertEqual(
            self._events("import.started")[0].actor,
            {"type": "staff", "id": self.op.public_id},
        )

    def test_actor_is_system_without_started_by(self):
        self._batch(CSV3, mapping=MAP, started_by=None)
        self._run()
        self.assertEqual(
            self._events("import.completed")[0].actor, {"type": "system"}
        )


class ChunkingTests(_Base):
    def test_chunks_advance_the_cursor_and_start_fires_once(self):
        body = b"name,slug\n" + b"".join(
            f"L{i},l{i}\n".encode() for i in range(1, 6)
        )
        batch = self._batch(body, mapping={"name": "name", "slug": "slug"})

        s1 = self._run(limit=2)
        batch.refresh_from_db()
        self.assertEqual((s1.rows_processed, s1.completed), (2, False))
        self.assertEqual(batch.status, ImportBatch.Status.PROCESSING)
        self.assertEqual(batch.processed_row_count, 2)

        self._run(limit=2)
        self._run(limit=2)
        batch.refresh_from_db()

        self.assertEqual(batch.status, ImportBatch.Status.COMPLETED)
        self.assertEqual(batch.processed_row_count, 5)
        self.assertEqual(batch.created_count, 5)
        self.assertEqual(len(self._events("import.started")), 1)
        self.assertEqual(len(self._events("import.completed")), 1)

    def test_nothing_claimable_is_a_noop(self):
        stats = import_once(now=timezone.now())
        self.assertEqual(stats, ImportStats())


class SuppressionTests(_Base):
    def test_a_suppressed_row_is_counted_not_created_not_errored(self):
        SuppressionKey.all_tenants.create(
            tenant=self.tenant,
            key_hash=fingerprint(name="Beta", locality=""),
            source_listing_public_id="listing_gone",
        )
        self._batch(CSV3, mapping=MAP, started_by=self.op)

        self._run()

        batch = ImportBatch.all_tenants.get(tenant=self.tenant)
        self.assertEqual(batch.suppressed_count, 1)
        self.assertEqual(batch.created_count, 2)
        self.assertEqual(batch.error_count, 0)
        self.assertNotIn("beta", self._listings())
        self.assertEqual(
            self._events("import.completed")[0].data["suppressed"], 1
        )

    def test_suppression_check_is_off_by_default(self):
        # A manual/API create is never suppressed -- only the importer opts in.
        from directory import services

        SuppressionKey.all_tenants.create(
            tenant=self.tenant,
            key_hash=fingerprint(name="Delta", locality=""),
            source_listing_public_id="x",
        )
        with tenant_context(self.tenant):
            result = services.upsert_listing(
                self.tenant, listing_type=self.lt,
                payload={"name": "Delta", "slug": "delta"},
                actor={"type": "admin", "id": "op_1"}, source="manual",
            )
        self.assertEqual(result.outcome, "created")


class PerRowErrorTests(_Base):
    def test_unknown_category_is_a_row_error_not_a_batch_failure(self):
        body = (
            b"name,slug,cat\nOne,one,plumbers\nTwo,two,ghosts\nThree,three,plumbers\n"
        )
        self._batch(body, mapping={"name": "name", "slug": "slug",
                                   "cat": "categories"})

        self._run()

        batch = ImportBatch.all_tenants.get(tenant=self.tenant)
        self.assertEqual(batch.status, ImportBatch.Status.COMPLETED)
        self.assertEqual(batch.created_count, 2)
        self.assertEqual(batch.error_count, 1)
        self.assertEqual(batch.errors[0]["row"], 2)
        self.assertIn("ghosts", str(batch.errors[0]))
        self.assertEqual(
            self._events("import.completed")[0].data["status"], "completed"
        )

    def test_nameless_row_is_an_error_and_fingerprint_is_not_called(self):
        body = b"name,slug\nReal,real\n   ,blank\n"
        self._batch(body, mapping={"name": "name", "slug": "slug"})

        # no exception escapes; fingerprint() would raise on a blank name
        self._run()

        batch = ImportBatch.all_tenants.get(tenant=self.tenant)
        self.assertEqual(batch.error_count, 1)
        self.assertEqual(batch.suppressed_count, 0)
        self.assertEqual(batch.created_count, 1)

    def test_malformed_phone_is_a_row_error_not_a_batch_failure(self):
        body = (
            b"name,slug,phone\n"
            b"Good,good,+13125550100\n"
            b"Bad,bad,not-a-phone\n"
            b"After,after,+13125550111\n"
        )
        self._batch(body, mapping={"name": "name", "slug": "slug",
                                   "phone": "contact.phone_e164"})

        self._run()

        batch = ImportBatch.all_tenants.get(tenant=self.tenant)
        self.assertEqual(batch.status, ImportBatch.Status.COMPLETED)
        self.assertEqual(batch.created_count, 2)   # rows 1 and 3
        self.assertEqual(batch.error_count, 1)     # row 2
        self.assertEqual(batch.errors[0]["row"], 2)
        self.assertIn("after", self._listings())   # row 3 still ran


class FatalRowTests(_Base):
    def test_an_unexpected_exception_fails_the_batch_and_earlier_rows_stand(self):
        from directory import services

        real = services.upsert_listing
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("db exploded")
            return real(*args, **kwargs)

        body = b"name,slug\nGood,good\nBoom,boom\nAfter,after\n"
        self._batch(body, mapping={"name": "name", "slug": "slug"},
                    started_by=self.op)

        with mock.patch("directory.importing.upsert_listing", side_effect=flaky):
            stats = self._run()

        self.assertTrue(stats.completed)
        batch = ImportBatch.all_tenants.get(tenant=self.tenant)
        self.assertEqual(batch.status, ImportBatch.Status.FAILED)
        self.assertEqual(batch.processed_row_count, 2)
        self.assertIn("good", self._listings())      # row 1 stands
        self.assertNotIn("after", self._listings())  # row 3 never ran
        completed = self._events("import.completed")[0]
        self.assertEqual(completed.data["status"], "failed")
        self.assertEqual(completed.data["created"], 1)
        self.assertIn("RuntimeError", completed.data["errors"][-1]["message"])

    def test_unreadable_file_fails_the_whole_batch(self):
        batch = self._batch(b"name,slug\nx,y\n",
                            mapping={"name": "name", "slug": "slug"})
        ImportBatch.all_tenants.filter(pk=batch.pk).update(
            stored_path="imports/gone.csv"
        )

        self._run()

        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.FAILED)
        self.assertEqual(len(self._events("import.started")), 1)
        self.assertEqual(
            self._events("import.completed")[0].data["status"], "failed"
        )
        self.assertIn("message", batch.errors[-1])


class ResumeTests(_Base):
    def test_replayed_row_is_a_note_not_an_error(self):
        batch = self._batch(b"name,slug\nRow,row\n",
                            mapping={"name": "name", "slug": "slug"})
        # a prior worker life applied row 1 but died before advancing the cursor
        CommandLog.objects.create(
            command="listing.upsert", tenant=self.tenant,
            idempotency_key=f"csv:{batch.public_id}:row_1", outcome="applied",
        )

        self._run()

        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.COMPLETED)
        self.assertEqual(batch.updated_count, 1)   # replayed counts as updated
        self.assertEqual(batch.error_count, 0)
        self.assertEqual(batch.errors, [])
        self.assertEqual(
            batch.notes,
            [{"row": 1, "note": "reprocessed after worker restart"}],
        )
