"""Tick jobs owned by the directory app.

Registered by ``audit.worker.jobs.build_tick_registry``, never at import time.
Each takes ``now`` as a required keyword and does a bounded amount of work per
call (``audit.worker.tick`` explains why). They are cross-tenant: they read
tenant-scoped models through ``all_tenants`` and enter a tenant explicitly.
"""

from __future__ import annotations

from django.db import transaction

from audit.worker.tick import TickResult
from directory import importing, lead_notices, sitemaps
from directory.models import Lead
from directory.search import drain_reindex_markers
from osds.tenancy import tenant_context
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


# Notices queued per call. A recipient is one message; the limit only bounds a
# backlog after the worker has been down, and ``more`` keeps the job due until
# it clears.
LEAD_FLUSH_LIMIT = 200


def lead_notice_flush(*, now) -> TickResult:
    """Decisions.md §4.10: announce leads a notice throttle deferred.

    The inline notice is leading-edge, so a lead arriving inside a recipient's
    15-minute window is not mailed and, with no later lead, would never be. This
    job finds every tenant with a non-spam lead inside the notice lifetime and
    runs the same count-since-last-notice routine the inline path uses, so a
    lead is announced once the window has passed, and never twice. When there is
    nothing unreported it queues nothing.
    """
    tenant_ids = set(
        Lead.all_tenants.filter(
            marked_spam=False, created_at__gt=now - lead_notices.OWNER_NOTICE_TTL
        ).values_list("tenant_id", flat=True)
    )
    queued = 0
    more = False
    tenants = Tenant.objects.filter(pk__in=tenant_ids).exclude(
        status=Tenant.Status.SUSPENDED
    )
    for tenant in tenants.order_by("id"):
        remaining = LEAD_FLUSH_LIMIT - queued
        if remaining <= 0:
            more = True
            break
        with tenant_context(tenant), transaction.atomic():
            sent, hit_limit = lead_notices.flush_tenant(
                tenant, now=now, limit=remaining
            )
        queued += sent
        more = more or hit_limit
    return TickResult(done=queued, more=more)
