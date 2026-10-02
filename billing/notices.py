"""The owner's billing mail (decisions.md §4.11).

Sent in the transaction that moved the entitlement, by ``apply_trigger``, so a
notice never describes a state that rolled back. Recipient: the listing's
current owner (the user row's address). The messages carry the site name, the
listing's name and a link to the owner's page, never anything a claimant wrote.

Each has a producer deadline -- past it the drain marks the row expired instead
of sending a stale "fix your card" after an outage:

* payment failed: until the dunning window ends;
* downgraded: until grace ends;
* expired: seven days;
* cancelled: until the period ends, else seven days.

A comp ending, and an operator's override, send nothing: comp status is not
shown to the owner (spec §6.5), and an override is the operator's own act.
"""

from __future__ import annotations

from datetime import timedelta

from audit.mail import enqueue
from directory import routing

KIND_FAILED = "billing.payment_failed"
KIND_DOWNGRADED = "billing.downgraded"
KIND_EXPIRED = "billing.expired"
KIND_CANCELED = "billing.canceled"

EXPIRED_TTL = timedelta(days=7)
# Expiry causes that are not the owner's business.
_QUIET_CAUSES = frozenset({"revoked", "comp_expired"})


def _link(tenant, listing) -> str:
    if routing.has_absolute_base(tenant):
        return routing.absolute_url(tenant, f"/owner/listings/{listing.public_id}/")
    return ""


def _send(tenant, listing, *, kind, subject, lines, expires_at) -> None:
    owner = listing.owner if listing.owner_id else None
    if owner is None or not owner.email:
        return
    url = _link(tenant, listing)
    body = "\n\n".join([*lines, f"Manage it here: {url}" if url else "Sign in to your owner page to manage it."])
    enqueue(
        tenant=tenant, kind=kind, to_address=owner.email, subject=subject,
        body_text=body, expires_at=expires_at,
    )


def after_transition(tenant, ent, listing, rule, *, now) -> None:
    from billing.machine import CANCELED, DOWNGRADED, DUNNING_STARTED, EXPIRED

    name = f"{listing.name} on {tenant.name}"
    events = set(rule.events)

    if DUNNING_STARTED in events and ent.dunning_ends_at:
        _send(
            tenant, listing, kind=KIND_FAILED,
            subject=f"Payment failed for {listing.name}",
            lines=[
                f"We could not take payment for {name}. Your listing keeps its "
                f"current perks while you fix it.",
                f"Please update your payment details before "
                f"{ent.dunning_ends_at:%Y-%m-%d} UTC; after that the listing is "
                f"downgraded.",
            ],
            expires_at=ent.dunning_ends_at,
        )
    if DOWNGRADED in events and ent.grace_ends_at:
        _send(
            tenant, listing, kind=KIND_DOWNGRADED,
            subject=f"{listing.name} has been downgraded",
            lines=[
                f"Payment for {name} is still outstanding, so the listing has been "
                f"downgraded and its perks are paused. It is still published.",
                f"You can restore it until {ent.grace_ends_at:%Y-%m-%d} UTC.",
            ],
            expires_at=ent.grace_ends_at,
        )
    if CANCELED in events:
        end = ent.current_period_end if ent.cancel_at_period_end else None
        lines = (
            [f"Your subscription for {name} is cancelled. It stays active until "
             f"{end:%Y-%m-%d} UTC."]
            if end and end > now
            else [f"Your subscription for {name} is cancelled and has ended."]
        )
        _send(
            tenant, listing, kind=KIND_CANCELED,
            subject=f"Subscription cancelled for {listing.name}", lines=lines,
            expires_at=end if end and end > now else now + EXPIRED_TTL,
        )
    elif EXPIRED in events and rule.expired_cause not in _QUIET_CAUSES:
        _send(
            tenant, listing, kind=KIND_EXPIRED,
            subject=f"{listing.name} is back on the free listing",
            lines=[
                f"The paid placement for {name} has ended. The listing is still "
                f"published; its upgraded perks have stopped.",
            ],
            expires_at=now + EXPIRED_TTL,
        )
