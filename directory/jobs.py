"""Tick jobs owned by the directory app.

Registered by ``audit.worker.jobs.build_tick_registry``, never at import time.
Each takes ``now`` as a required keyword and does a bounded amount of work per
call (``audit.worker.tick`` explains why). They are cross-tenant: they read
tenant-scoped models through ``all_tenants`` and enter a tenant explicitly.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Q

from audit.worker.tick import TickResult
from directory import claim_review, importing, lead_notices, sitemaps
from directory.models import Claim, Lead, ModerationItem
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


def claim_notice_flush(*, now) -> TickResult:
    """Decisions.md §4.6 (#241): announce claims the operator-notice throttle
    deferred.

    The inline notice is leading-edge, so a claim entering review inside a
    recipient's 15-minute window is not mailed and, with no later claim, would
    never be. This job finds every tenant with a claim in review or an open
    dispute inside the notice lifetime and tells each recipient whose window
    has passed and who has not yet heard about an arrival. When there is
    nothing unreported it queues nothing.
    """
    cutoff = now - claim_review.OPERATOR_NOTICE_TTL
    tenant_ids = set(
        Claim.all_tenants.filter(
            status=Claim.Status.PENDING_VERIFICATION, updated_at__gt=cutoff
        ).values_list("tenant_id", flat=True)
    ) | set(
        ModerationItem.all_tenants.filter(
            status=ModerationItem.Status.OPEN,
            item_type=ModerationItem.ItemType.CLAIM_DISPUTE,
            created_at__gt=cutoff,
        ).values_list("tenant_id", flat=True)
    )
    queued = 0
    tenants = Tenant.objects.filter(pk__in=tenant_ids).exclude(
        status=Tenant.Status.SUSPENDED
    )
    for tenant in tenants.order_by("id"):
        with tenant_context(tenant), transaction.atomic():
            queued += claim_review.flush_operator_notices(tenant, now=now)
    return TickResult(done=queued, more=False)


# Rows per call, like the audit sweeps.
OWNER_AUTH_CHUNK = 1000
# A spent or expired link and a dead session are kept a day past their end,
# for diagnosing "my link did not work", then deleted.
OWNER_AUTH_KEEP = timedelta(days=1)


def owner_auth_prune(*, now) -> TickResult:
    """Decisions.md §4.9: delete sign-in links and owner sessions that can no
    longer be used. Cross-tenant by nature, so it reads through
    ``all_tenants``; bounded to one chunk a call per table, and ``more`` while
    either has backlog."""
    from directory.models import OwnerSession, OwnerSignInToken
    from directory.owner_auth import SESSION_IDLE

    cutoff = now - OWNER_AUTH_KEEP
    token_ids = list(
        OwnerSignInToken.all_tenants.filter(expires_at__lt=cutoff)
        .order_by("id").values_list("id", flat=True)[:OWNER_AUTH_CHUNK]
    )
    session_ids = list(
        OwnerSession.all_tenants.filter(
            Q(expires_at__lt=cutoff) | Q(last_seen_at__lt=cutoff - SESSION_IDLE)
        )
        .order_by("id").values_list("id", flat=True)[:OWNER_AUTH_CHUNK]
    )
    deleted_tokens, _ = OwnerSignInToken.all_tenants.filter(id__in=token_ids).delete()
    deleted_sessions, _ = OwnerSession.all_tenants.filter(id__in=session_ids).delete()
    return TickResult(
        done=deleted_tokens + deleted_sessions,
        more=len(token_ids) == OWNER_AUTH_CHUNK or len(session_ids) == OWNER_AUTH_CHUNK,
    )
