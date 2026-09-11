"""CSV import PR 3: batch rollback.

``TransactionTestCase`` throughout -- the capture path runs through
``upsert_listing`` (``require_autocommit``) and ``rollback_import_batch`` is a
command orchestrator that does the same. The stored CSV is written to a real
per-tenant ``FileSystemStorage`` rooted at a temp dir.

Seven guards are red-checked out of band (see the PR notes), not here; the
tests below pin the behaviour those checks protect.
"""

from __future__ import annotations

import functools
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.contrib.postgres.search import SearchQuery
from django.core.files.base import ContentFile
from django.db.models import Max
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse as _reverse
from django.utils import timezone

from audit.models import CommandLog, OutboxEvent
from directory import services
from directory.importing import import_once, null_import_pre_images
from directory.models import (
    Category,
    Claim,
    DirectoryUser,
    ImportBatch,
    ImportBatchListing,
    Listing,
    ListingType,
)
from directory.storage import get_tenant_storage
from osds.tenancy import tenant_context
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant

reverse = functools.partial(_reverse, urlconf="osds.urls_tenant")
HOST = "acme.test"
_HASH = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_HASH
)
class _Base(TransactionTestCase):
    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="osds-rollback-test-")
        self.addCleanup(shutil.rmtree, self._root, ignore_errors=True)
        ctx = override_settings(OSDS_MEDIA_ROOT=self._root)
        ctx.enable()
        self.addCleanup(ctx.disable)
        notify = mock.patch("audit.outbox._notify_outbox")
        notify.start()
        self.addCleanup(notify.stop)

        InstallSetup.objects.create(
            token_hash="x" * 64, completed_at=timezone.now()
        )
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain=HOST
        )
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
            fields=[{"key": "licence", "label": "Licence", "type": "text",
                     "required": False, "searchable": False}],
        )
        Category.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="plumbers",
            name="Plumbers",
        )
        Category.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="electricians",
            name="Electricians",
        )
        self.manager = self._member("mgr@acme.test", StaffMembership.Role.MANAGER)
        self.editor = self._member("ed@acme.test", StaffMembership.Role.EDITOR)

    # --- fixtures --------------------------------------------------------

    def _member(self, email, role):
        op = Operator.objects.create_user(email=email, password="x")
        StaffMembership.objects.create(
            operator=op, tenant=self.tenant, role=role,
            status=StaffMembership.Status.ACTIVE,
        )
        return op

    def _existing(self, slug, *, name=None, locality="", categories=(),
                  custom=None):
        payload = {
            "slug": slug,
            "name": name or slug.title(),
            "location": {"locality": locality} if locality else {},
            "categories": list(categories),
        }
        if custom:
            payload["custom_fields"] = custom
        with tenant_context(self.tenant):
            result = services.upsert_listing(
                self.tenant, listing_type=self.lt, payload=payload,
                actor={"type": "admin", "id": "op_seed"}, source="manual",
                must_create=True,
            )
        return result.listing

    def _batch(self, body: bytes, *, mapping, status=ImportBatch.Status.PENDING):
        headers = body.decode("utf-8-sig").splitlines()[0].split(",")
        key = get_tenant_storage(self.tenant).save(
            "imports/in.csv", ContentFile(body)
        )
        return ImportBatch.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, source="csv",
            status=status, stored_path=key, original_filename="in.csv",
            detected_headers=headers, delimiter=",", encoding="utf-8-sig",
            has_header=True, column_mapping=mapping, started_by=self.manager,
        )

    def _drain_imports(self):
        for _ in range(50):
            if import_once(now=timezone.now(), limit=50).batch_id is None:
                return
        raise AssertionError("imports did not drain")

    def _rollback(self, batch, operator=None):
        with tenant_context(self.tenant):
            return services.rollback_import_batch(
                batch, operator=operator or self.manager
            )

    # --- queries --------------------------------------------------------

    def _listings(self):
        return {
            l.slug: l
            for l in Listing.all_tenants.filter(tenant=self.tenant)
        }

    def _rows(self, batch):
        return list(
            ImportBatchListing.all_tenants.filter(batch=batch).order_by("id")
        )

    def _events(self, etype, *, after=0):
        return list(
            OutboxEvent.all_tenants.filter(type=etype, id__gt=after).order_by(
                "id"
            )
        )

    def _event_high(self):
        return OutboxEvent.all_tenants.aggregate(m=Max("id"))["m"] or 0


UPD_MAP = {"name": "name", "slug": "slug", "city": "location.locality"}


# =========================================================================
# pre-image capture (in _apply_upsert)
# =========================================================================
class PreImageCaptureTests(_Base):
    def test_update_row_captures_pre_image(self):
        self._existing("acme", name="Acme", locality="Chicago")
        self._batch(b"name,slug,city\nAcme,acme,Peoria\n", mapping=UPD_MAP)

        self._drain_imports()

        row = ImportBatchListing.all_tenants.get(listing__slug="acme")
        self.assertEqual(row.action, "updated")
        self.assertEqual(row.pre_image["name"], "Acme")
        self.assertEqual(row.pre_image["location"]["locality"], "Chicago")
        self.assertIsNone(row.pre_image_nulled_at)
        # the listing did get the imported value
        self.assertEqual(self._listings()["acme"].locality, "Peoria")

    def test_create_row_records_created_action_with_no_pre_image(self):
        self._batch(b"name,slug,city\nBeta,beta,Rome\n", mapping=UPD_MAP)

        self._drain_imports()

        row = ImportBatchListing.all_tenants.get(listing__slug="beta")
        self.assertEqual(row.action, "created")
        self.assertIsNone(row.pre_image)

    def test_unchanged_row_writes_no_join_row(self):
        self._existing("acme", name="Acme", locality="Chicago")
        # identical values -> upsert returns "unchanged"
        self._batch(b"name,slug,city\nAcme,acme,Chicago\n", mapping=UPD_MAP)

        self._drain_imports()

        self.assertEqual(
            ImportBatchListing.all_tenants.filter(listing__slug="acme").count(),
            0,
        )

    def test_pre_image_is_a_projection_not_a_patch(self):
        self._existing("acme", name="Acme", locality="Chicago")
        self._batch(b"name,slug,city\nAcme,acme,Peoria\n", mapping=UPD_MAP)

        self._drain_imports()

        pre = ImportBatchListing.all_tenants.get(listing__slug="acme").pre_image
        self.assertIsInstance(pre, dict)
        self.assertLessEqual(
            {"slug", "name", "location", "contact", "custom_fields",
             "categories", "provenance"},
            set(pre),
        )
        self.assertNotIn("op", pre)  # not an RFC 6902 op

    def test_pre_image_excludes_lifecycle_state(self):
        listing = self._existing("acme", name="Acme", locality="Chicago")
        with tenant_context(self.tenant):
            services.set_listing_visibility(
                listing, Listing.Visibility.PUBLISHED,
                actor={"type": "admin", "id": "op_seed"},
            )
        self._batch(b"name,slug,city\nAcme,acme,Peoria\n", mapping=UPD_MAP)

        self._drain_imports()

        pre = ImportBatchListing.all_tenants.get(listing__slug="acme").pre_image
        for absent in ("id", "status", "visibility", "tier", "owner"):
            self.assertNotIn(absent, pre)

    def test_capture_is_transactional_with_the_update(self):
        self._existing("acme", name="Acme", locality="Chicago")
        self._batch(b"name,slug,city\nAcme,acme,Peoria\n", mapping=UPD_MAP)
        real = services.emit

        def boom(event_type, **kw):
            if event_type == "listing.updated":
                raise RuntimeError("kaboom")
            return real(event_type, **kw)

        with mock.patch("directory.services.emit", side_effect=boom):
            self._drain_imports()

        # the update rolled back -> no listing change, no orphan provenance row
        self.assertEqual(self._listings()["acme"].locality, "Chicago")
        self.assertEqual(
            ImportBatchListing.all_tenants.filter(listing__slug="acme").count(),
            0,
        )
        self.assertEqual(
            ImportBatch.all_tenants.get().status, ImportBatch.Status.FAILED
        )

    def test_first_touch_wins_when_a_batch_creates_then_updates_a_row(self):
        # two CSV rows, same slug: row 1 creates, row 2 matches and updates.
        body = b"name,slug,city\nAcme,acme,Chicago\nAcme Renamed,acme,Peoria\n"
        self._batch(body, mapping=UPD_MAP)

        self._drain_imports()

        row = ImportBatchListing.all_tenants.get(listing__slug="acme")
        self.assertEqual(row.action, "created")
        self.assertIsNone(row.pre_image)

    def test_first_touch_wins_when_a_batch_updates_the_same_row_twice(self):
        # two CSV rows, same slug, both matching a pre-existing listing: the
        # join row keeps the FIRST row's pre-image -- the listing's true
        # pre-batch state -- not the state left by the first row.
        self._existing("keep", name="Keep", locality="Old")
        body = b"name,slug,city\nKeep,keep,First\nKeep,keep,Second\n"
        batch = self._batch(body, mapping=UPD_MAP)

        self._drain_imports()
        batch = ImportBatch.all_tenants.get(pk=batch.pk)

        rows = ImportBatchListing.all_tenants.filter(listing__slug="keep")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.action, "updated")
        self.assertEqual(row.pre_image["location"]["locality"], "Old")
        # the second row wins the listing's live value
        self.assertEqual(self._listings()["keep"].locality, "Second")

        self._rollback(batch)

        # restored to the true pre-batch state, not to "First"
        self.assertEqual(self._listings()["keep"].locality, "Old")


# =========================================================================
# rollback: apply
# =========================================================================
class RollbackApplyTests(_Base):
    def _mixed_batch(self):
        """One batch that updates 'keep' (Old -> New) and creates 'fresh'."""
        self._existing("keep", name="Keep", locality="Old",
                       categories=["plumbers"])
        body = b"name,slug,city\nKeep,keep,New\nFresh,fresh,Berlin\n"
        batch = self._batch(body, mapping=UPD_MAP)
        self._drain_imports()
        return ImportBatch.all_tenants.get(pk=batch.pk)

    def test_deletes_created_and_restores_updated(self):
        batch = self._mixed_batch()
        self.assertEqual(self._listings()["keep"].locality, "New")

        self._rollback(batch)

        listings = self._listings()
        self.assertNotIn("fresh", listings)
        self.assertEqual(listings["keep"].locality, "Old")
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.ROLLED_BACK)
        self.assertEqual(batch.rolled_back_by, self.manager)
        self.assertIsNotNone(batch.rolled_back_at)

    def test_emits_exactly_one_event(self):
        batch = self._mixed_batch()
        high = self._event_high()

        self._rollback(batch)

        self.assertEqual(len(self._events("import.rolled_back", after=high)), 1)
        for noisy in ("listing.updated", "listing.deleted", "listing.created"):
            self.assertEqual(self._events(noisy, after=high), [])

    def test_event_payload(self):
        batch = self._mixed_batch()
        high = self._event_high()

        self._rollback(batch)

        data = self._events("import.rolled_back", after=high)[0].data
        self.assertEqual(data["batch_id"], batch.public_id)
        self.assertEqual(data["listings_removed"], 1)
        self.assertEqual(data["listings_restored"], 1)
        self.assertEqual(data["rolled_back_by"], self.manager.public_id)

    def test_writes_no_suppression_key(self):
        from directory.models import SuppressionKey

        batch = self._mixed_batch()
        self._rollback(batch)
        self.assertEqual(
            SuppressionKey.all_tenants.filter(tenant=self.tenant).count(), 0
        )

    def test_event_and_state_commit_together(self):
        batch = self._mixed_batch()
        real = services.emit

        def boom(event_type, **kw):
            if event_type == "import.rolled_back":
                raise RuntimeError("kaboom")
            return real(event_type, **kw)

        with mock.patch("directory.services.emit", side_effect=boom):
            with self.assertRaises(RuntimeError):
                self._rollback(batch)

        listings = self._listings()
        self.assertIn("fresh", listings)            # delete rolled back
        self.assertEqual(listings["keep"].locality, "New")  # restore rolled back
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.COMPLETED)
        row = CommandLog.objects.get(command="import.rollback")
        self.assertFalse(row.outcome)               # threw mid-apply

    def test_restore_recomputes_the_search_vector(self):
        self._existing("acme", name="Original Plumbing", locality="Chicago")
        self._batch(b"name,slug,city\nImported Widgets,acme,Chicago\n",
                    mapping=UPD_MAP)
        self._drain_imports()
        batch = ImportBatch.all_tenants.get()
        pk = Listing.all_tenants.get(slug="acme").pk

        def matches(term):
            return Listing.all_tenants.filter(
                pk=pk, search_vector=SearchQuery(term, config="english")
            ).exists()

        self.assertTrue(matches("widgets"))
        self.assertFalse(matches("original"))

        self._rollback(batch)

        self.assertTrue(matches("original"))
        self.assertFalse(matches("widgets"))

    def test_restore_reverts_categories(self):
        self._existing("keep", name="Keep", categories=["plumbers"])
        # remap categories column so the import can change them
        m = {"name": "name", "slug": "slug", "cats": "categories"}
        self._batch(b"name,slug,cats\nKeep,keep,electricians\n", mapping=m)
        self._drain_imports()
        batch = ImportBatch.all_tenants.get()
        with tenant_context(self.tenant):
            self.assertEqual(
                {c.slug for c in self._listings()["keep"].categories.all()},
                {"electricians"},
            )

        self._rollback(batch)

        with tenant_context(self.tenant):
            self.assertEqual(
                {c.slug for c in self._listings()["keep"].categories.all()},
                {"plumbers"},
            )

    def test_command_log_pair(self):
        batch = self._mixed_batch()
        high = self._event_high()

        event_id = self._rollback(batch)

        row = CommandLog.objects.get(command="import.rollback")
        self.assertEqual(row.outcome, "applied")
        self.assertEqual(row.tenant, self.tenant)
        self.assertEqual(row.result_event_id, event_id)
        self.assertEqual(
            self._events("import.rolled_back", after=high)[0].event_id, event_id
        )

    def test_pre_images_are_nulled_after_rollback(self):
        batch = self._mixed_batch()
        self._rollback(batch)
        for row in self._rows(batch):
            self.assertIsNone(row.pre_image)
            self.assertIsNotNone(row.pre_image_nulled_at)


# =========================================================================
# rollback: guards
# =========================================================================
class RollbackGuardTests(_Base):
    def _one_update_batch(self):
        self._existing("keep", name="Keep", locality="Old")
        self._batch(b"name,slug,city\nKeep,keep,New\n", mapping=UPD_MAP)
        self._drain_imports()
        return ImportBatch.all_tenants.get()

    def test_refused_while_processing(self):
        batch = self._one_update_batch()
        ImportBatch.all_tenants.filter(pk=batch.pk).update(
            status=ImportBatch.Status.PROCESSING
        )
        batch.refresh_from_db()
        high = self._event_high()

        with self.assertRaises(services.RollbackRefused) as cm:
            self._rollback(batch)

        self.assertEqual(cm.exception.reason, "not_rollbackable")
        self.assertEqual(self._listings()["keep"].locality, "New")
        self.assertEqual(self._events("import.rolled_back", after=high), [])
        self.assertEqual(
            CommandLog.objects.get(command="import.rollback").outcome, "rejected"
        )

    def test_refused_when_pre_images_nulled(self):
        batch = self._one_update_batch()
        ImportBatchListing.all_tenants.filter(batch=batch).update(
            pre_image_nulled_at=timezone.now()
        )
        high = self._event_high()

        with self.assertRaises(services.RollbackRefused) as cm:
            self._rollback(batch)

        self.assertEqual(cm.exception.reason, "past_rollback_window")
        self.assertEqual(self._listings()["keep"].locality, "New")
        self.assertEqual(self._events("import.rolled_back", after=high), [])

    def test_refused_when_a_created_listing_carries_a_claim(self):
        body = b"name,slug,city\nKeep,keep,New\nFresh,fresh,Berlin\n"
        self._existing("keep", name="Keep", locality="Old")
        self._batch(body, mapping=UPD_MAP)
        self._drain_imports()
        batch = ImportBatch.all_tenants.get()

        fresh = Listing.all_tenants.get(slug="fresh")
        claimant = DirectoryUser.all_tenants.create(
            tenant=self.tenant, email="c@x.test"
        )
        Claim.all_tenants.create(
            tenant=self.tenant, listing=fresh, claimant=claimant,
            method=Claim.Method.MANUAL,
        )
        high = self._event_high()

        with self.assertRaises(services.RollbackRefused) as cm:
            self._rollback(batch)

        self.assertEqual(cm.exception.reason, "created_listing_claimed")
        self.assertIn("Fresh", cm.exception.message)
        self.assertIn(fresh.public_id, cm.exception.message)
        # nothing happened
        self.assertIn("fresh", self._listings())
        self.assertEqual(self._listings()["keep"].locality, "New")
        self.assertEqual(self._events("import.rolled_back", after=high), [])

    def test_second_rollback_is_refused(self):
        batch = self._one_update_batch()
        high = self._event_high()
        self._rollback(batch)

        with self.assertRaises(services.RollbackRefused) as cm:
            self._rollback(batch)

        self.assertEqual(cm.exception.reason, "not_rollbackable")
        self.assertEqual(
            len(self._events("import.rolled_back", after=high)), 1
        )

    def test_edit_after_import_is_restored_over(self):
        batch = self._one_update_batch()
        listing = Listing.all_tenants.get(slug="keep")
        with tenant_context(self.tenant):
            services.upsert_listing(
                self.tenant, listing_type=self.lt,
                payload={"id": listing.public_id, "slug": "keep",
                         "name": "Keep", "location": {"locality": "HandEdited"}},
                actor={"type": "admin", "id": "op_seed"}, source="manual",
            )
        self.assertEqual(self._listings()["keep"].locality, "HandEdited")

        self._rollback(batch)

        self.assertEqual(self._listings()["keep"].locality, "Old")

    def test_hand_deleted_updated_row_is_skipped(self):
        batch = self._one_update_batch()
        Listing.all_tenants.filter(tenant=self.tenant, slug="keep").delete()
        high = self._event_high()

        event_id = self._rollback(batch)

        data = self._events("import.rolled_back", after=high)[0].data
        self.assertEqual(data["listings_restored"], 0)
        self.assertEqual(data["listings_removed"], 0)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.ROLLED_BACK)
        self.assertTrue(event_id)

    def test_failed_batch_is_rollbackable(self):
        real = services.upsert_listing
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("db exploded")
            return real(*a, **kw)

        body = b"name,slug,city\nGood,good,A\nBoom,boom,B\nAfter,after,C\n"
        self._batch(body, mapping=UPD_MAP)
        with mock.patch("directory.importing.upsert_listing", side_effect=flaky):
            self._drain_imports()
        batch = ImportBatch.all_tenants.get()
        self.assertEqual(batch.status, ImportBatch.Status.FAILED)
        self.assertIn("good", self._listings())
        high = self._event_high()

        self._rollback(batch)

        self.assertNotIn("good", self._listings())
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.ROLLED_BACK)
        self.assertEqual(
            self._events("import.rolled_back", after=high)[0].data[
                "listings_removed"
            ],
            1,
        )


# =========================================================================
# pre-image nulling (a pure function; not a tick job)
# =========================================================================
class NullPreImagesTests(_Base):
    def _completed_batch(self, slug, *, age_days):
        self._existing(slug, name=slug.title(), locality="Old")
        self._batch(
            f"name,slug,city\n{slug.title()},{slug},New\n".encode(),
            mapping=UPD_MAP,
        )
        self._drain_imports()
        batch = ImportBatch.all_tenants.get(row_provenance__listing__slug=slug)
        ImportBatch.all_tenants.filter(pk=batch.pk).update(
            completed_at=timezone.now() - timedelta(days=age_days)
        )
        return batch

    def test_nulls_pre_images_older_than_90_days(self):
        old = self._completed_batch("old", age_days=91)
        recent = self._completed_batch("recent", age_days=10)

        n = null_import_pre_images(now=timezone.now())

        self.assertEqual(n, 1)
        old_row = ImportBatchListing.all_tenants.get(batch=old)
        self.assertIsNone(old_row.pre_image)
        self.assertIsNotNone(old_row.pre_image_nulled_at)
        recent_row = ImportBatchListing.all_tenants.get(batch=recent)
        self.assertIsNotNone(recent_row.pre_image)
        self.assertIsNone(recent_row.pre_image_nulled_at)

    def test_nulling_is_idempotent(self):
        self._completed_batch("old", age_days=91)
        null_import_pre_images(now=timezone.now())
        stamp = ImportBatchListing.all_tenants.get().pre_image_nulled_at

        self.assertEqual(null_import_pre_images(now=timezone.now()), 0)
        self.assertEqual(
            ImportBatchListing.all_tenants.get().pre_image_nulled_at, stamp
        )

    def test_nulled_batch_cannot_roll_back(self):
        batch = self._completed_batch("old", age_days=91)
        null_import_pre_images(now=timezone.now())

        with self.assertRaises(services.RollbackRefused) as cm:
            self._rollback(batch)
        self.assertEqual(cm.exception.reason, "past_rollback_window")

    def _failed_update_batch(self, *, age_days):
        """A batch that updates 'keep' in row 1, then fails on row 2, so
        _finish(status='failed') runs and stamps completed_at."""
        self._existing("keep", name="Keep", locality="Old")
        real = services.upsert_listing
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("db exploded")
            return real(*a, **kw)

        self._batch(b"name,slug,city\nKeep,keep,New\nBoom,boom,B\n",
                    mapping=UPD_MAP)
        with mock.patch("directory.importing.upsert_listing", side_effect=flaky):
            self._drain_imports()
        batch = ImportBatch.all_tenants.get()
        ImportBatch.all_tenants.filter(pk=batch.pk).update(
            completed_at=timezone.now() - timedelta(days=age_days)
        )
        return batch

    def test_nulls_pre_images_for_a_failed_batch(self):
        batch = self._failed_update_batch(age_days=91)
        self.assertEqual(batch.status, ImportBatch.Status.FAILED)
        self.assertIsNotNone(batch.completed_at)  # _finish set it on the failed path

        n = null_import_pre_images(now=timezone.now())

        self.assertEqual(n, 1)
        row = ImportBatchListing.all_tenants.get(
            batch=batch, listing__slug="keep"
        )
        self.assertEqual(row.action, "updated")
        self.assertIsNone(row.pre_image)
        self.assertIsNotNone(row.pre_image_nulled_at)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.FAILED)


# =========================================================================
# the admin action
# =========================================================================
class AdminRollbackViewTests(_Base):
    def _client(self, operator):
        c = Client()
        c.force_login(operator)
        return c

    def _url(self, name, **kw):
        return reverse(f"directory_admin:{name}", kwargs=kw)

    def _completed_batch(self):
        self._existing("keep", name="Keep", locality="Old")
        self._batch(b"name,slug,city\nKeep,keep,New\nFresh,fresh,B\n",
                    mapping=UPD_MAP)
        self._drain_imports()
        return ImportBatch.all_tenants.get()

    def test_manager_can_roll_back(self):
        batch = self._completed_batch()
        resp = self._client(self.manager).post(
            self._url("import-rollback", public_id=batch.public_id),
            HTTP_HOST=HOST,
        )
        self.assertEqual(resp.status_code, 302)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.ROLLED_BACK)
        self.assertNotIn("fresh", self._listings())

    def test_editor_cannot_roll_back(self):
        batch = self._completed_batch()
        resp = self._client(self.editor).post(
            self._url("import-rollback", public_id=batch.public_id),
            HTTP_HOST=HOST,
        )
        self.assertEqual(resp.status_code, 403)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.COMPLETED)

    def test_detail_shows_the_button_only_when_rollbackable(self):
        batch = self._completed_batch()
        c = self._client(self.manager)
        action = self._url("import-rollback", public_id=batch.public_id)

        body = c.get(
            self._url("import-detail", public_id=batch.public_id),
            HTTP_HOST=HOST,
        ).content.decode()
        self.assertIn(action, body)

        ImportBatch.all_tenants.filter(pk=batch.pk).update(
            status=ImportBatch.Status.PROCESSING
        )
        body = c.get(
            self._url("import-detail", public_id=batch.public_id),
            HTTP_HOST=HOST,
        ).content.decode()
        self.assertNotIn(action, body)

    def test_detail_button_requires_manager_role(self):
        # import_rollback is gated at manager; the detail page must not offer
        # the button to a rank the view would 403.
        batch = self._completed_batch()
        action = self._url("import-rollback", public_id=batch.public_id)
        detail_url = self._url("import-detail", public_id=batch.public_id)

        editor_body = self._client(self.editor).get(
            detail_url, HTTP_HOST=HOST
        ).content.decode()
        self.assertNotIn(action, editor_body)

        manager_body = self._client(self.manager).get(
            detail_url, HTTP_HOST=HOST
        ).content.decode()
        self.assertIn(action, manager_body)

    def test_detail_explains_a_passed_window(self):
        batch = self._completed_batch()
        ImportBatchListing.all_tenants.filter(batch=batch).update(
            pre_image_nulled_at=timezone.now()
        )
        body = self._client(self.manager).get(
            self._url("import-detail", public_id=batch.public_id),
            HTTP_HOST=HOST,
        ).content.decode()
        self.assertNotIn(
            self._url("import-rollback", public_id=batch.public_id), body
        )
        self.assertIn("90-day rollback window", body)

    def test_refused_rollback_surfaces_the_message(self):
        batch = self._completed_batch()
        fresh = Listing.all_tenants.get(slug="fresh")
        claimant = DirectoryUser.all_tenants.create(
            tenant=self.tenant, email="c@x.test"
        )
        Claim.all_tenants.create(
            tenant=self.tenant, listing=fresh, claimant=claimant,
            method=Claim.Method.MANUAL,
        )
        resp = self._client(self.manager).post(
            self._url("import-rollback", public_id=batch.public_id),
            HTTP_HOST=HOST, follow=True,
        )
        self.assertContains(resp, "carry a claim")
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.COMPLETED)
