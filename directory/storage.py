"""Per-tenant media storage, resolved at runtime.

``django-storages`` reads ``settings.STORAGES`` at import time, but the backend
choice and its credentials are wizard-entered and live in the database
(decisions.md §4). ``get_tenant_storage`` builds a Django ``Storage`` for one
tenant from ``tenant.settings["storage"]`` on each call.

Only the local-disk backend is wired today. ``s3``, ``azure`` and ``gcp`` are
configurable in the first-run wizard but raise :class:`DeferredFeatureError`
until their SDKs land (#150) -- each is a runtime dependency that needs a
human.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage, Storage

_CLOUD_BACKENDS = {"s3", "azure", "gcp"}


class DeferredFeatureError(RuntimeError):
    """A storage backend that is offered in the wizard but not yet implemented."""


def tenant_storage_backend(tenant) -> str:
    """The configured backend name for ``tenant``; ``"local"`` when unset."""
    cfg = (getattr(tenant, "settings", None) or {}).get("storage") or {}
    return cfg.get("backend") or "local"


def get_tenant_storage(tenant) -> Storage:
    """A ``Storage`` rooted at this tenant's media area.

    Local disk keys off ``settings.OSDS_MEDIA_ROOT`` with a per-tenant
    subdirectory named by the tenant's public id, so one tenant's keys can
    never resolve into another's tree.
    """
    backend = tenant_storage_backend(tenant)
    if backend == "local":
        location = Path(settings.OSDS_MEDIA_ROOT) / tenant.public_id
        return FileSystemStorage(location=str(location))
    if backend in _CLOUD_BACKENDS:
        raise DeferredFeatureError(
            f"the {backend!r} storage backend is configured for this tenant but "
            f"is not available yet (#150); only local-disk storage is wired. "
            f"Change the storage backend in the admin, or wait for the release "
            f"that adds it."
        )
    raise DeferredFeatureError(f"unknown storage backend: {backend!r}")
