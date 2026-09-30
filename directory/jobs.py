"""Tick jobs owned by the directory app.

Registered by ``audit.worker.jobs.build_tick_registry``, never at import time.
Each takes ``now`` as a required keyword and does a bounded amount of work per
call (``audit.worker.tick`` explains why). They are cross-tenant: they read
tenant-scoped models through ``all_tenants`` and enter a tenant explicitly.
"""

from __future__ import annotations

from audit.worker.tick import TickResult
from directory import importing, sitemaps
from directory.search import drain_reindex_markers
from tenants.models import Tenant


def search_reindex(*, now) -> TickResult:
    """#132: drain pending SearchReindexJob markers, one bounded chunk a call."""
    result = drain_reindex_markers(now=now)
    return TickResult(done=result.listings + result.markers_done, more=result.more)


# Rows per call. A pre-image is a full listing projection, so a chunk is kept
# smaller than the audit sweeps'.
PRE_IMAGE_CHUNK = 500


def import_pre_image_retention(*, now) -> TickResult:
    """#193: null import pre-images past their 90-day window (spec §3.3, §11.2)."""
    done = importing.null_import_pre_images(now=now, limit=PRE_IMAGE_CHUNK)
    return TickResult(done=done, more=done == PRE_IMAGE_CHUNK)


def sitemap_regeneration(*, now) -> TickResult:
    """#159: keep each tenant's stored sitemap current.

    Every call computes a cheap fingerprint per tenant; it renders **at most one
    tenant** per call, because a render walks every published listing and the
    tick shares a thread with the mail drain. ``more`` is set while another
    tenant is waiting to rebuild, so a backlog clears at pass speed. A tenant
    changed inside its five-minute debounce window is not backlog: it is picked
    up by a later tick.
    """
    rebuilt = 0
    waiting = False
    for tenant in Tenant.objects.exclude(status=Tenant.Status.SUSPENDED).order_by("id"):
        if rebuilt:
            # Already spent this call's render; note whether this tenant also
            # needs one, without doing it.
            if sitemaps.needs_rebuild(tenant, now=now):
                waiting = True
                break
            continue
        if sitemaps.refresh(tenant, now=now) == "rebuilt":
            rebuilt += 1
    return TickResult(done=rebuilt, more=waiting)
