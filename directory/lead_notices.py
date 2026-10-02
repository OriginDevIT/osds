"""Who is told about a new lead, and how (decisions.md §4.10).

``notify_for_lead`` is called from inside ``create_lead``'s transaction, so a
notice commits with the lead that caused it (decisions.md §4.3). Every notice is
an ``OutboundMessage`` with a deadline, and none of them carries visitor-supplied
text: anyone can write anything into the form, and a notice goes out from the
operator's domain.

* A listing **with an owner**: the owner is told ("N new inquiries"), with a
  link to ``/owner/`` when the tenant has an absolute base. The owner's leads
  page is where the inquiries are read.
* A listing **with no owner**: only the staff who can act (EDITOR and above)
  are told, with a link to the admin leads page. An unclaimed listing's own
  email address is never mailed -- it was imported, not given.
* The visitor is never mailed: anyone can name an address, and a confirmation
  to it would let the form flood a stranger (decisions.md §4.4, "Sends are
  capped per pair").

A recipient is told at most once per ``NOTICE_THROTTLE``, a leading-edge
throttle derived from recent ``OutboundMessage`` rows with no table of its own.
A notice counts every non-spam lead since the previous one, so a throttled lead
is deferred, not lost -- and ``flush_tenant``, run by the worker's
``lead_notice_flush`` tick job, announces it once the window has passed even if
no further lead ever arrives. Both paths use the same routines, so a lead is
reported exactly once: it is counted by the first notice created after it.
"""

from __future__ import annotations

from datetime import timedelta

from audit.mail import enqueue
from audit.models import OutboundMessage
from directory import routing
from directory.models import DirectoryUser, Lead
from tenants.models import StaffMembership

OWNER_NOTICE_KIND = "lead.owner_notice"
OPERATOR_NOTICE_KIND = "lead.operator_notice"
OWNER_NOTICE_TTL = timedelta(days=7)
OPERATOR_NOTICE_TTL = timedelta(hours=48)
NOTICE_THROTTLE = timedelta(minutes=15)
NOTICE_ROLE = StaffMembership.Role.EDITOR


def _link(tenant, path: str) -> "str | None":
    return routing.absolute_url(tenant, path) if routing.has_absolute_base(tenant) else None


def _last_notice(tenant, *, kind: str, address: str):
    return (
        OutboundMessage.all_tenants.filter(tenant=tenant, kind=kind, to_address=address)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )


def _noun(count: int) -> str:
    return "inquiry" if count == 1 else "inquiries"


def notify_for_lead(tenant, *, listing, now) -> int:
    """Queue the notice a new, non-spam lead calls for. Returns the number of
    messages queued (0 when throttled or when there is no one to tell)."""
    owner = listing.owner
    if owner is not None and (owner.email or "").strip():
        return _notify_owner(tenant, recipient=owner, now=now)
    return _notify_operators(tenant, now=now)


def _notify_owner(tenant, *, recipient, now) -> int:
    """Tell ``recipient`` about leads on their listings since their last notice,
    unless one was sent inside the throttle or there is nothing new."""
    address = recipient.email.strip().lower()
    last = _last_notice(tenant, kind=OWNER_NOTICE_KIND, address=address)
    if last is not None and last > now - NOTICE_THROTTLE:
        return 0

    since = last or (now - OWNER_NOTICE_TTL)
    leads = Lead.objects.filter(
        listing__owner=recipient, marked_spam=False, created_at__gt=since
    )
    count = leads.count()
    if count == 0:
        return 0
    listings = list(leads.values_list("listing", "listing__name").distinct()[:2])
    if len(listings) == 1:
        where = f"on {listings[0][1]}"
    else:
        where = f"across your listings on {tenant.name}"
    url = _link(tenant, "/owner/")
    link_line = f"\n\nSee them at: {url}" if url else (
        f"\n\nSign in to your owner page on {tenant.name} to see them."
    )
    body = (
        f"You have {count} new {_noun(count)} {where}."
        f"{link_line}"
        f"\n\nYou get at most one of these every 15 minutes, so the number "
        f"may be higher by the time you look."
    )
    enqueue(
        tenant=tenant,
        kind=OWNER_NOTICE_KIND,
        to_address=address,
        subject=f"New {_noun(count)} on {tenant.name}",
        body_text=body,
        expires_at=now + OWNER_NOTICE_TTL,
    )
    return 1


def _notify_operators(tenant, *, now) -> int:
    recipients: list[str] = []
    for membership in (
        StaffMembership.objects.filter(
            tenant=tenant,
            status=StaffMembership.Status.ACTIVE,
            role__gte=NOTICE_ROLE,
            operator__is_active=True,
        )
        .select_related("operator")
        .order_by("id")
    ):
        address = (membership.operator.email or "").strip().lower()
        if address and address not in recipients:
            recipients.append(address)

    queued = 0
    for address in recipients:
        last = _last_notice(tenant, kind=OPERATOR_NOTICE_KIND, address=address)
        if last is not None and last > now - NOTICE_THROTTLE:
            continue
        since = last or (now - OPERATOR_NOTICE_TTL)
        count = Lead.objects.filter(
            listing__owner__isnull=True, marked_spam=False, created_at__gt=since
        ).count()
        if count == 0:
            continue
        url = _link(tenant, "/admin/leads/")
        link_line = f"\n\nSee them at: {url}" if url else ""
        body = (
            f"{count} new {_noun(count)} arrived on listings that no one has "
            f"claimed on {tenant.name}."
            f"{link_line}"
            f"\n\nYou get at most one of these every 15 minutes, so the number "
            f"may be higher by the time you look."
        )
        enqueue(
            tenant=tenant,
            kind=OPERATOR_NOTICE_KIND,
            to_address=address,
            subject=f"New {_noun(count)} on unclaimed listings at {tenant.name}",
            body_text=body,
            expires_at=now + OPERATOR_NOTICE_TTL,
        )
        queued += 1
    return queued


def flush_tenant(tenant, *, now, limit: int) -> "tuple[int, bool]":
    """Announce leads a throttle deferred, for one tenant. Call with the tenant
    in scope and inside a transaction (``enqueue`` requires one).

    Each owner with an unreported lead inside the notice lifetime, and each staff
    recipient when there are unreported leads on unclaimed listings, goes through
    the same routine the inline path uses -- so nothing is announced twice, and a
    recipient still inside their window, or with nothing new, is left alone.
    Returns ``(messages queued, more)``; ``more`` is true when ``limit`` stopped
    the owners loop with owners still to look at.
    """
    queued = 0
    owners = list(
        DirectoryUser.objects.filter(
            owned_listings__leads__marked_spam=False,
            owned_listings__leads__created_at__gt=now - OWNER_NOTICE_TTL,
        )
        .distinct()
        .order_by("id")
    )
    for owner in owners:
        if queued >= limit:
            return queued, True
        queued += _notify_owner(tenant, recipient=owner, now=now)

    if Lead.objects.filter(
        listing__owner__isnull=True,
        marked_spam=False,
        created_at__gt=now - OPERATOR_NOTICE_TTL,
    ).exists():
        queued += _notify_operators(tenant, now=now)
    return queued, False
