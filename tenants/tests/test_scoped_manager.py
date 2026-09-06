"""App-level tenant scoping is the only isolation OSDS has (CLAUDE.md
invariant 3, decisions.md §4). These tests are the floor: every tenant-scoped
model must use the scoped default manager, keep an unscoped base manager, and
expose the ``all_tenants`` escape hatch -- unless it is on the allowlist below,
each entry justified.
"""

from __future__ import annotations

from django.apps import apps
from django.db import models
from django.test import SimpleTestCase, TestCase

from osds.db import TenantScopedManager
from osds.tenancy import NoTenantInScope, tenant_context

_PROJECT_APPS = {"tenants", "directory", "billing", "audit"}

# Models that carry a ``tenant`` FK but deliberately use a plain manager.
# label -> reason (asserted non-trivial).
_ALLOWLIST = {
    "tenants.StaffMembership": (
        "Carries tenant because it *is* the operator-tenant relationship "
        "(CLAUDE.md invariant 3), not tenant-owned data. The console lists an "
        "operator's memberships across every tenant, with none in scope, so a "
        "scoped default manager would break it."
    ),
    "audit.CommandLog": (
        "Written outside the command transaction, before a tenant is "
        "necessarily resolved; tenant is nullable by the spec §11.2 bounded "
        "exception. A scoped manager would raise while logging a malformed "
        "command."
    ),
    "audit.AccessLog": (
        "Records console and superadmin access, which has no tenant; the "
        "tenant FK is nullable and rows are written without a tenant in scope."
    ),
    "tenants.Secret": (
        "Resolution deliberately spans a tenant-scoped row and a "
        "deployment-level row (tenant IS NULL); the lookup in tenants.secrets "
        "queries both scopes explicitly, so a tenant-scoped default manager "
        "would hide the deployment fallback."
    ),
}


def _tenant_model():
    return apps.get_model("tenants", "Tenant")


def _is_tenant_scoped(model) -> bool:
    try:
        field = model._meta.get_field("tenant")
    except Exception:
        return False
    return isinstance(field, models.ForeignKey) and field.related_model is _tenant_model()


def _project_models():
    for model in apps.get_models():
        if model._meta.app_label in _PROJECT_APPS:
            yield model


class ScopedManagerConfigurationTests(SimpleTestCase):
    def test_the_check_actually_inspects_tenant_scoped_models(self):
        # Guard against a refactor that makes the assertions below vacuous.
        scoped = [m._meta.label for m in _project_models() if _is_tenant_scoped(m)]
        self.assertGreaterEqual(len(scoped), 12, scoped)

    def test_every_tenant_scoped_model_has_the_scoped_default_manager(self):
        offenders = [
            m._meta.label
            for m in _project_models()
            if _is_tenant_scoped(m)
            and m._meta.label not in _ALLOWLIST
            and not isinstance(m._default_manager, TenantScopedManager)
        ]
        self.assertEqual(
            offenders,
            [],
            "these models carry a tenant FK but their default manager is not "
            "TenantScopedManager; scope them or add a justified allowlist entry",
        )

    def test_scoped_models_keep_an_unscoped_base_manager(self):
        # Reverse-relation traversal and the delete collector use _base_manager;
        # if it filtered, cascades would silently drop rows.
        for m in _project_models():
            if not _is_tenant_scoped(m):
                continue
            self.assertNotIsInstance(
                m._base_manager, TenantScopedManager, m._meta.label
            )

    def test_scoped_models_expose_the_all_tenants_escape_hatch(self):
        for m in _project_models():
            if not _is_tenant_scoped(m) or m._meta.label in _ALLOWLIST:
                continue
            manager = getattr(m, "all_tenants", None)
            self.assertIsInstance(manager, models.Manager, m._meta.label)
            self.assertNotIsInstance(
                manager, TenantScopedManager, m._meta.label
            )

    def test_allowlist_entries_exist_and_are_justified(self):
        for label, reason in _ALLOWLIST.items():
            app_label, model_name = label.split(".")
            apps.get_model(app_label, model_name)  # raises if renamed or removed
            self.assertGreaterEqual(len(reason.strip()), 60, label)

    def test_allowlisted_models_use_a_plain_manager(self):
        for label in _ALLOWLIST:
            model = apps.get_model(*label.split("."))
            self.assertNotIsInstance(
                model._default_manager, TenantScopedManager, label
            )


class ScopedManagerBehaviourTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from tenants.models import Tenant

        cls.t1 = Tenant.objects.create(slug="t1", name="Tenant One")
        cls.t2 = Tenant.objects.create(slug="t2", name="Tenant Two")

        from directory.models import Listing, ListingType

        cls.lt1 = ListingType.all_tenants.create(
            tenant=cls.t1,
            key="business",
            label_singular="Business",
            label_plural="Businesses",
            path_segment="businesses",
        )
        cls.lt2 = ListingType.all_tenants.create(
            tenant=cls.t2,
            key="business",
            label_singular="Business",
            label_plural="Businesses",
            path_segment="businesses",
        )
        Listing.all_tenants.create(
            tenant=cls.t1, listing_type=cls.lt1, slug="a", name="A"
        )
        Listing.all_tenants.create(
            tenant=cls.t2, listing_type=cls.lt2, slug="b", name="B"
        )

    def test_query_without_a_tenant_in_scope_raises(self):
        from directory.models import Listing

        with self.assertRaises(NoTenantInScope):
            list(Listing.objects.all())

    def test_query_is_filtered_to_the_tenant_in_scope(self):
        from directory.models import Listing

        with tenant_context(self.t1):
            self.assertEqual([o.slug for o in Listing.objects.all()], ["a"])
        with tenant_context(self.t2):
            self.assertEqual([o.slug for o in Listing.objects.all()], ["b"])

    def test_all_tenants_manager_crosses_the_boundary(self):
        from directory.models import Listing

        self.assertEqual(Listing.all_tenants.count(), 2)

    def test_scope_is_restored_after_the_context_exits(self):
        from directory.models import Listing

        with tenant_context(self.t1):
            list(Listing.objects.all())
        with self.assertRaises(NoTenantInScope):
            list(Listing.objects.all())
