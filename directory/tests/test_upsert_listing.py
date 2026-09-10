"""directory.services.upsert_listing -- create / update / unchanged, rejected
fields, categories, normalisation, idempotency + the command log, and the
autocommit guard.

upsert_listing refuses to run inside an open transaction, so these tests use
TransactionTestCase (a plain TestCase wraps every test in one).
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.db import transaction
from django.test import TestCase, TransactionTestCase

from audit.models import CommandLog, OutboxEvent
from directory import services
from directory.field_schema import SchemaError
from directory.models import Category, Listing, ListingType
from directory.services import MustNotBeInTransaction, RejectedField
from osds.tenancy import tenant_context
from tenants.models import Operator, Tenant

ACTOR = {"type": "admin", "id": "op_test"}


class _Base(TransactionTestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
            fields=[
                {"key": "years", "label": "Years", "type": "integer", "required": False},
            ],
        )
        self.lt2 = ListingType.all_tenants.create(
            tenant=self.tenant, key="software", label_singular="S",
            label_plural="Ss", path_segment="software",
        )
        for slug in ("plumbers", "roofers"):
            Category.all_tenants.create(
                tenant=self.tenant, listing_type=self.lt, slug=slug, name=slug.title()
            )

    def upsert(self, payload, **kw):
        kw.setdefault("actor", ACTOR)
        kw.setdefault("source", "manual")
        with tenant_context(self.tenant):
            return services.upsert_listing(
                self.tenant, listing_type=self.lt, payload=payload, **kw
            )

    def events(self, etype):
        return OutboxEvent.all_tenants.filter(type=etype, tenant=self.tenant)


class CreateUpdateUnchangedTests(_Base):
    def test_create_emits_listing_created_with_type_key(self):
        result = self.upsert(
            {"slug": "acme-co", "name": "Acme Co", "categories": ["plumbers"]}
        )
        self.assertEqual(result.outcome, "created")
        event = self.events("listing.created").get()
        self.assertEqual(event.subject, result.listing.public_id)
        self.assertEqual(event.data["type"], "business")
        self.assertEqual(event.data["categories"], ["plumbers"])

    def test_create_requires_slug_and_name(self):
        with self.assertRaises(SchemaError):
            self.upsert({"slug": "x"})

    def test_update_emits_patch(self):
        self.upsert({"slug": "acme-co", "name": "Acme Co"})
        result = self.upsert({"slug": "acme-co", "name": "Acme Corp"})
        self.assertEqual(result.outcome, "updated")
        self.assertEqual(
            result.changes, [{"op": "replace", "path": "/name", "value": "Acme Corp"}]
        )
        self.assertEqual(self.events("listing.updated").get().data["changes"], result.changes)

    def test_identical_reupsert_is_unchanged_and_emits_nothing(self):
        self.upsert({"slug": "acme-co", "name": "Acme Co", "categories": ["plumbers"]})
        result = self.upsert(
            {"slug": "acme-co", "name": "Acme Co", "categories": ["plumbers"]}
        )
        self.assertEqual(result.outcome, "unchanged")
        self.assertIsNone(result.event_id)
        self.assertEqual(self.events("listing.updated").count(), 0)

    def test_normalised_difference_is_still_unchanged(self):
        self.upsert(
            {"slug": "acme-co", "name": "Acme Co", "contact": {"email": "a@b.co"}}
        )
        result = self.upsert(
            {
                "slug": "acme-co",
                "name": "  Acme Co  ",
                "contact": {"email": "A@B.CO"},
            }
        )
        self.assertEqual(result.outcome, "unchanged")

    def test_match_is_per_listing_type(self):
        self.upsert({"slug": "shared", "name": "In business"})
        with tenant_context(self.tenant):
            result = services.upsert_listing(
                self.tenant,
                listing_type=self.lt2,
                payload={"slug": "shared", "name": "In software"},
                actor=ACTOR,
                source="manual",
            )
        self.assertEqual(result.outcome, "created")
        self.assertEqual(Listing.all_tenants.filter(slug="shared").count(), 2)


class RejectedFieldTests(_Base):
    def test_visibility_tier_status_media_are_rejected_even_when_null(self):
        # media is owned by directory.media, not writable through upsert
        # (spec §7.1, v0.7).
        for field in ("visibility", "tier", "status", "media"):
            with self.subTest(field=field):
                with self.assertRaises(RejectedField) as ctx:
                    self.upsert({"slug": "x", "name": "X", field: None})
                self.assertEqual(ctx.exception.field, field)

    def test_unknown_category_is_rejected(self):
        with self.assertRaises(SchemaError):
            self.upsert({"slug": "x", "name": "X", "categories": ["ghost"]})

    def test_unknown_custom_field_is_rejected(self):
        with self.assertRaises(SchemaError):
            self.upsert(
                {"slug": "x", "name": "X", "custom_fields": {"bogus": 1}}
            )

    def test_id_match_with_wrong_listing_type_is_rejected(self):
        created = self.upsert({"slug": "acme-co", "name": "Acme Co"})
        with tenant_context(self.tenant):
            with self.assertRaises(RejectedField) as ctx:
                services.upsert_listing(
                    self.tenant,
                    listing_type=self.lt2,
                    payload={"id": created.listing.public_id, "name": "x"},
                    actor=ACTOR,
                    source="manual",
                )
        self.assertEqual(ctx.exception.field, "listing_type")

    def test_geo_precision_none_with_coords_is_rejected(self):
        with self.assertRaises(SchemaError):
            self.upsert(
                {
                    "slug": "x",
                    "name": "X",
                    "location": {"lat": 41.9, "lon": -87.6, "geo_precision": "none"},
                }
            )

    def test_malformed_phone_is_a_schema_error_not_a_valueerror(self):
        with self.assertRaises(SchemaError):
            self.upsert(
                {"slug": "x", "name": "X", "contact": {"phone_e164": "not-a-phone"}}
            )

    def test_malformed_coordinate_is_a_schema_error_not_a_valueerror(self):
        with self.assertRaises(SchemaError):
            self.upsert(
                {"slug": "x", "name": "X", "location": {"lat": "north-ish"}}
            )

    def test_coords_without_precision_default_to_locality(self):
        result = self.upsert(
            {"slug": "x", "name": "X", "location": {"lat": 41.9, "lon": -87.6}}
        )
        result.listing.refresh_from_db()
        self.assertEqual(result.listing.geo_precision, "locality")

    def test_must_create_conflict_links_to_existing(self):
        self.upsert({"slug": "dup", "name": "First"})
        with self.assertRaises(SchemaError) as ctx:
            self.upsert({"slug": "dup", "name": "Second"}, must_create=True)
        self.assertIn("already exists", str(ctx.exception))


class IdempotencyTests(_Base):
    def test_replay_of_an_applied_write_returns_the_original_event_id(self):
        first = self.upsert(
            {"slug": "acme-co", "name": "Acme Co"}, idempotency_key="csv:row1"
        )
        replay = self.upsert(
            {"slug": "acme-co", "name": "TOTALLY DIFFERENT"}, idempotency_key="csv:row1"
        )
        self.assertEqual(replay.outcome, "replayed")
        self.assertEqual(replay.event_id, first.event_id)
        # the divergent payload was not applied
        first.listing.refresh_from_db()
        self.assertEqual(first.listing.name, "Acme Co")

    def test_command_log_records_applied_with_the_event_id(self):
        result = self.upsert(
            {"slug": "acme-co", "name": "Acme Co"}, idempotency_key="k1"
        )
        row = CommandLog.objects.get(idempotency_key="k1", problem__isnull=True)
        self.assertEqual(row.outcome, "applied")
        self.assertEqual(row.result_event_id, result.event_id)
        self.assertIsNotNone(row.concluded_at)

    def test_unchanged_keyed_write_logs_applied_with_null_event_id(self):
        self.upsert({"slug": "acme-co", "name": "Acme Co"}, idempotency_key="k2a")
        result = self.upsert(
            {"slug": "acme-co", "name": "Acme Co"}, idempotency_key="k2b"
        )
        self.assertEqual(result.outcome, "unchanged")
        row = CommandLog.objects.get(idempotency_key="k2b")
        self.assertEqual(row.outcome, "applied")
        self.assertEqual(row.result_event_id, "")

    def test_replay_of_an_unchanged_write_returns_no_event_id(self):
        # create, then an unchanged keyed write (concludes applied, event id NULL)
        self.upsert({"slug": "acme-co", "name": "Acme Co"}, idempotency_key="k3-create")
        self.upsert({"slug": "acme-co", "name": "Acme Co"}, idempotency_key="k3-noop")
        # replay of the no-op: detected, but carries no event id -> the caller
        # answers 200, not 409 (ruling 5)
        replay = self.upsert(
            {"slug": "acme-co", "name": "Acme Co"}, idempotency_key="k3-noop"
        )
        self.assertEqual(replay.outcome, "replayed")
        self.assertIsNone(replay.event_id)

    def test_rejected_write_is_logged_as_rejected(self):
        with self.assertRaises(RejectedField):
            self.upsert(
                {"slug": "x", "name": "X", "status": "claimed"}, idempotency_key="k4"
            )
        row = CommandLog.objects.get(idempotency_key="k4")
        self.assertEqual(row.outcome, "rejected")
        self.assertEqual(row.problem, {"field": "status"})


class MidApplyCrashTests(_Base):
    def test_command_log_survives_a_service_raising_mid_apply(self):
        with mock.patch(
            "directory.services.emit", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.upsert(
                    {"slug": "acme-co", "name": "Acme Co"}, idempotency_key="crash1"
                )
        row = CommandLog.objects.get(idempotency_key="crash1")
        self.assertIsNone(row.outcome)  # threw mid-apply
        self.assertIsNone(row.concluded_at)
        # the listing write rolled back
        self.assertFalse(Listing.all_tenants.filter(slug="acme-co").exists())


class JsonablePayloadTests(_Base):
    """A payload value that is not JSON-native (a Decimal coordinate from the
    admin form, a date) must be coerced before it reaches the command log --
    the JSONField uses the stdlib encoder (spec §7.1, §11.2)."""

    def test_decimal_coordinates_are_logged_as_numbers_and_applied(self):
        result = self.upsert(
            {
                "slug": "acme-co",
                "name": "Acme Co",
                "location": {
                    "lat": Decimal("41.850000"),
                    "lon": Decimal("-87.650000"),
                    "geo_precision": "locality",
                },
            },
            idempotency_key="dec1",
        )
        self.assertEqual(result.outcome, "created")
        result.listing.refresh_from_db()
        self.assertEqual(result.listing.lat, Decimal("41.850000"))
        self.assertEqual(result.listing.lon, Decimal("-87.650000"))

        row = CommandLog.objects.get(idempotency_key="dec1", problem__isnull=True)
        self.assertEqual(row.outcome, "applied")
        lat = row.payload["location"]["lat"]
        self.assertIsInstance(lat, float)  # a JSON number, not "41.85"
        self.assertEqual(lat, 41.85)

    def test_non_finite_coordinate_is_a_422_and_is_logged_rejected(self):
        with self.assertRaises(SchemaError):
            self.upsert(
                {
                    "slug": "x",
                    "name": "X",
                    "location": {"lat": float("nan"), "lon": 1.0},
                },
                idempotency_key="nan1",
            )
        # jsonb cannot store nan/inf; the attempt is refused, still logged
        row = CommandLog.objects.get(idempotency_key="nan1")
        self.assertEqual(row.outcome, "rejected")
        self.assertIn("payload", row.problem)
        self.assertIsNone(row.payload)
        self.assertFalse(Listing.all_tenants.filter(slug="x").exists())


class TransactionGuardTests(TestCase):
    """A plain TestCase runs inside a transaction, which is exactly what the
    guard forbids."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")
        cls.lt = ListingType.all_tenants.create(
            tenant=cls.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )

    def test_call_inside_a_transaction_is_refused(self):
        with tenant_context(self.tenant):
            with self.assertRaises(MustNotBeInTransaction):
                services.upsert_listing(
                    self.tenant, listing_type=self.lt,
                    payload={"slug": "x", "name": "X"},
                    actor=ACTOR, source="manual",
                )

    def test_explicit_atomic_wrapper_is_also_refused(self):
        with tenant_context(self.tenant), transaction.atomic():
            with self.assertRaises(MustNotBeInTransaction):
                services.upsert_listing(
                    self.tenant, listing_type=self.lt,
                    payload={"slug": "x", "name": "X"},
                    actor=ACTOR, source="manual",
                )
