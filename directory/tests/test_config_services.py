"""directory.services -- listing type and category config through the service
layer, emitting tenant.settings_changed.
"""

from __future__ import annotations

from django.test import TestCase

from audit.models import OutboxEvent
from directory import services
from directory.field_schema import SchemaError
from directory.models import Category, ListingType, Listing, PathRedirect
from osds.tenancy import tenant_context
from tenants.models import Operator, Tenant


class ConfigServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")
        cls.actor = Operator.objects.create_user(
            email="admin@acme.test", password="x"
        )

    def _events(self, **filt):
        return OutboxEvent.all_tenants.filter(
            type="tenant.settings_changed", tenant=self.tenant, **filt
        )

    def _last_change(self):
        return self._events().order_by("-id").first().data["changes"][0]

    def _make_type(self, key="business", segment="businesses", fields=None):
        with tenant_context(self.tenant):
            return services.create_listing_type(
                self.tenant,
                key=key,
                label_singular=key.title(),
                label_plural=key.title() + "s",
                path_segment=segment,
                claimable=True,
                fields=fields or [],
                actor=self.actor,
            )

    # --- create -------------------------------------------------------------
    def test_create_emits_add_at_the_type_pointer(self):
        lt = self._make_type()
        change = self._last_change()
        self.assertEqual(change["op"], "add")
        self.assertEqual(change["path"], "/listing_types/business")
        self.assertEqual(change["value"]["path_segment"], "businesses")
        self.assertEqual(self._events().last().data["changed_by"], self.actor.public_id)

    def test_create_rejects_an_invalid_schema_without_writing(self):
        with tenant_context(self.tenant):
            with self.assertRaises(SchemaError):
                services.create_listing_type(
                    self.tenant,
                    key="bad",
                    label_singular="Bad",
                    label_plural="Bads",
                    path_segment="bads",
                    claimable=True,
                    fields=[{"key": "status", "label": "S", "type": "text"}],
                    actor=self.actor,
                )
        self.assertFalse(ListingType.all_tenants.filter(key="bad").exists())
        self.assertFalse(self._events().exists())

    # --- frozen key -------------------------------------------------------
    def test_key_is_frozen_on_update(self):
        lt = self._make_type()
        with tenant_context(self.tenant):
            with self.assertRaises(ValueError):
                services.update_listing_type(lt, key="renamed", actor=self.actor)
        lt.refresh_from_db()
        self.assertEqual(lt.key, "business")

    def test_update_emits_replace(self):
        lt = self._make_type()
        with tenant_context(self.tenant):
            services.update_listing_type(
                lt, label_plural="Companies", actor=self.actor
            )
        change = self._last_change()
        self.assertEqual(change["op"], "replace")
        self.assertEqual(change["path"], "/listing_types/business")
        self.assertEqual(change["value"]["label_plural"], "Companies")

    def test_update_fields_runs_frozen_type_check(self):
        lt = self._make_type(fields=[{"key": "years", "label": "Years", "type": "integer"}])
        with tenant_context(self.tenant):
            with self.assertRaises(SchemaError):
                services.update_listing_type(
                    lt,
                    fields=[{"key": "years", "label": "Years", "type": "text"}],
                    actor=self.actor,
                )

    # --- path_segment redirects (spec §4.5) --------------------------------
    def test_segment_change_writes_no_redirect_while_single_type(self):
        lt = self._make_type()
        with tenant_context(self.tenant):
            services.update_listing_type(lt, path_segment="companies", actor=self.actor)
        self.assertFalse(PathRedirect.all_tenants.filter(tenant=self.tenant).exists())

    def test_second_type_records_the_root_redirect(self):
        self._make_type(key="business", segment="businesses")
        self._make_type(key="software", segment="software")
        redirect = PathRedirect.all_tenants.get(tenant=self.tenant, old_prefix="")
        self.assertEqual(redirect.new_prefix, "/businesses")

    def test_segment_change_when_multi_type_records_a_redirect(self):
        b = self._make_type(key="business", segment="businesses")
        self._make_type(key="software", segment="software")
        with tenant_context(self.tenant):
            services.update_listing_type(b, path_segment="companies", actor=self.actor)
        redirect = PathRedirect.all_tenants.get(
            tenant=self.tenant, old_prefix="/businesses"
        )
        self.assertEqual(redirect.new_prefix, "/companies")

    # --- delete ---------------------------------------------------------
    def test_delete_type_with_listings_is_blocked(self):
        lt = self._make_type()
        Listing.all_tenants.create(
            tenant=self.tenant, listing_type=lt, slug="x", name="X"
        )
        with tenant_context(self.tenant):
            with self.assertRaises(ValueError):
                services.delete_listing_type(lt, actor=self.actor)
        self.assertTrue(ListingType.all_tenants.filter(pk=lt.pk).exists())

    def test_delete_empty_type_emits_remove(self):
        lt = self._make_type()
        with tenant_context(self.tenant):
            services.delete_listing_type(lt, actor=self.actor)
        self.assertFalse(ListingType.all_tenants.filter(pk=lt.pk).exists())
        change = self._last_change()
        self.assertEqual(change, {"op": "remove", "path": "/listing_types/business"})

    # --- categories ---------------------------------------------------------
    def test_create_category_emits_add_at_the_category_pointer(self):
        lt = self._make_type()
        with tenant_context(self.tenant):
            services.create_category(
                lt, name="Plumbers", slug="plumbers", parent=None, order=0,
                actor=self.actor,
            )
        change = self._last_change()
        self.assertEqual(change["op"], "add")
        self.assertEqual(change["path"], "/categories/business/plumbers")
        self.assertEqual(change["value"], {"name": "Plumbers", "parent": None, "order": 0})

    def test_delete_category_cascades_children_and_emits_remove(self):
        lt = self._make_type()
        with tenant_context(self.tenant):
            parent = services.create_category(
                lt, name="Trades", slug="trades", parent=None, order=0, actor=self.actor
            )
            child = services.create_category(
                lt, name="Plumbers", slug="plumbers", parent=parent, order=0,
                actor=self.actor,
            )
            services.delete_category(parent, actor=self.actor)
        self.assertFalse(Category.all_tenants.filter(pk=child.pk).exists())
        self.assertEqual(
            self._last_change(), {"op": "remove", "path": "/categories/business/trades"}
        )
