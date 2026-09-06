"""directory.services.set_listing_visibility -- only crossing the published
boundary emits an event (ruling 2).
"""

from __future__ import annotations

from django.test import TestCase

from audit.models import OutboxEvent
from directory import services
from directory.models import Listing, ListingType
from osds.tenancy import tenant_context
from tenants.models import Tenant

ACTOR = {"type": "admin", "id": "op_test"}


class SetVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")
        cls.lt = ListingType.all_tenants.create(
            tenant=cls.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )

    def _listing(self, visibility="draft"):
        return Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="x", name="X",
            visibility=visibility,
        )

    def _types(self, listing):
        return set(
            OutboxEvent.all_tenants.filter(
                subject=listing.public_id
            ).values_list("type", flat=True)
        )

    def _set(self, listing, visibility):
        with tenant_context(self.tenant):
            return services.set_listing_visibility(
                listing, visibility, actor=ACTOR
            )

    def test_draft_to_published_emits_published(self):
        listing = self._listing("draft")
        self._set(listing, "published")
        self.assertEqual(self._types(listing), {"listing.published"})
        event = OutboxEvent.all_tenants.get(type="listing.published")
        self.assertEqual(event.data["from"], "draft")

    def test_published_to_draft_emits_unpublished(self):
        listing = self._listing("published")
        self._set(listing, "draft")
        event = OutboxEvent.all_tenants.get(type="listing.unpublished")
        self.assertEqual(event.data, {
            "from": "published", "to": "draft", "reason": "request", "type": "business"
        })

    def test_hidden_to_published_emits_published(self):
        listing = self._listing("hidden")
        self._set(listing, "published")
        self.assertEqual(self._types(listing), {"listing.published"})

    def test_draft_to_hidden_emits_nothing(self):
        listing = self._listing("draft")
        self._set(listing, "hidden")
        self.assertEqual(self._types(listing), set())
        listing.refresh_from_db()
        self.assertEqual(listing.visibility, "hidden")

    def test_hidden_to_draft_emits_nothing(self):
        listing = self._listing("hidden")
        self._set(listing, "draft")
        self.assertEqual(self._types(listing), set())

    def test_noop_same_value(self):
        listing = self._listing("published")
        self._set(listing, "published")
        self.assertEqual(self._types(listing), set())

    def test_invalid_value_raises(self):
        listing = self._listing()
        with self.assertRaises(ValueError):
            self._set(listing, "archived")
