"""The tenant-scoped default manager.

Every model that carries a ``tenant`` FK declares, in this order::

    objects = TenantScopedManager()
    all_tenants = models.Manager()

``objects`` is declared first, so it is ``_default_manager``: it filters every
query to the tenant in ambient scope and raises if there is none.

``_base_manager`` is deliberately left as Django's default plain manager (no
``Meta.base_manager_name``) -- reverse-relation traversal and the delete
collector must never silently filter.

``all_tenants`` is the explicit, greppable escape hatch for the few legitimate
cross-tenant call sites: the installation console, the worker, data migrations.

``tenants/tests/test_scoped_manager.py`` enforces this wiring across every app.
"""

from __future__ import annotations

from django.db import models

from .tenancy import NoTenantInScope, get_current_tenant


class TenantScopedManager(models.Manager):
    # Migrations use the plain manager; a historical model has no ambient tenant.
    use_in_migrations = False

    def get_queryset(self) -> models.QuerySet:
        tenant = get_current_tenant()
        if tenant is None:
            raise NoTenantInScope(
                f"{self.model._meta.label} was queried with no tenant in scope. "
                f"Wrap the call in osds.tenancy.tenant_context(...), or use "
                f"{self.model.__name__}.all_tenants for a deliberate "
                f"cross-tenant query."
            )
        return super().get_queryset().filter(tenant=tenant)
