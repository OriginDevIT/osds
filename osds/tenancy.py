"""Ambient current-tenant scope.

App-level scoping is the only tenant isolation OSDS has (CLAUDE.md invariant 3):
one unscoped query on a tenant-scoped model is a cross-tenant leak. The scoped
default manager (``osds.db.TenantScopedManager``) reads the current tenant from
here.

The request middleware that sets this per request lands in a later PR. The
worker, management commands, the console and the tests set it explicitly with
``tenant_context()``.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tenants.models import Tenant

_current_tenant: "contextvars.ContextVar[Tenant | None]" = contextvars.ContextVar(
    "osds_current_tenant", default=None
)


class NoTenantInScope(RuntimeError):
    """Raised when a tenant-scoped model is queried with no tenant in scope."""


def get_current_tenant() -> "Tenant | None":
    return _current_tenant.get()


def set_current_tenant(tenant: "Tenant | None") -> contextvars.Token:
    """Set the current tenant, returning a token for :func:`reset_current_tenant`."""
    return _current_tenant.set(tenant)


def reset_current_tenant(token: contextvars.Token) -> None:
    _current_tenant.reset(token)


@contextmanager
def tenant_context(tenant: "Tenant | None") -> "Iterator[Tenant | None]":
    """Run a block with ``tenant`` in scope, restoring the previous value after."""
    token = _current_tenant.set(tenant)
    try:
        yield tenant
    finally:
        _current_tenant.reset(token)
