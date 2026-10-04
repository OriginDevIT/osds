"""Claim review: approval, rejection, disputes and the moderation queue
(claims PR 4a -- spec §9.3, §9.4, §9.6; decisions.md §4.4, §4.6).

This module holds the only code that writes ``Listing.owner`` and
``Listing.status``: ``_assign_owner``, reached from ``_approve_claim``, which
is called by operator approval and by code verification and by nothing else
(#48). ``directory/tests/test_claim_review.py`` fails if anything else assigns
either field.

Operator commands follow the orchestrator shape the rest of the service layer
uses: ``require_autocommit``, ``log_received``, an atomic ``_apply_*``, then
``log_conclude``. A refusal changes nothing and concludes ``rejected``; an
attempt by an operator below the required role concludes ``blocked``. Locks
are always taken in the order guard, claim, listing, item -- every command
takes a subsequence of it, so none can deadlock another.

Call with the tenant in ambient scope, like the rest of the service layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from audit import events
from audit.command_log import log_conclude, log_received, require_autocommit
from audit.mail import enqueue
from audit.models import OutboundMessage
from audit.outbox import emit
from directory import routing
from directory.masking import mask_email
from directory.models import (
    Claim,
    ClaimVerificationGuard,
    Listing,
    ModerationItem,
)
from tenants.models import StaffMembership

# Spec §9.6: three rejections on one (listing, email) pair block it.
REJECTION_BLOCK_THRESHOLD = 3

# Producer deadlines (decisions.md §4.6). Past them the drain marks the row
# expired and never sends: a stale "your listing was claimed" notice or a
# rejection reason delivered after an outage reads as current.
ANTI_HIJACK_NOTICE_TTL = timedelta(days=7)
REJECTION_NOTICE_TTL = timedelta(days=7)

LOSER_REASON = "Another claim for this listing was approved."

# The operator notice (#218, decisions.md §4.6): one message per recipient per
# window, whatever number of claims entered review inside it.
OPERATOR_NOTICE_KIND = "claim.review_notice"
OPERATOR_NOTICE_TTL = timedelta(hours=48)
OPERATOR_NOTICE_THROTTLE = timedelta(minutes=15)

# The decision recorded when an unlock or unblock finds nothing left to clear.
ALREADY_CLEARED = "already_cleared"

# Ranks, from spec §4.4: approving or rejecting a claim moves ownership of a
# business's page (editor); the moderation queue's own actions are the
# moderator's.
DECIDE_ROLE = StaffMembership.Role.EDITOR
QUEUE_ROLE = StaffMembership.Role.MODERATOR

OPEN_CLAIM_STATUSES = (Claim.Status.PENDING_VERIFICATION, Claim.Status.DISPUTED)


class ReviewRefused(Exception):
    """A guard refusal: nothing written. ``reason`` is a stable code the view
    maps to copy; ``forbidden`` concludes ``blocked``, the rest ``rejected``."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


REFUSAL_TEXT = {
    "forbidden": "Your role does not allow this action.",
    "status": "This claim has already been decided or is no longer open.",
    "notes_required": "Notes are required: record how the claim was verified.",
    "method_used_invalid": "Choose how the claim was verified.",
    "reason_required": "A reason is required: the claimant is told what was missing.",
    "rationale_required": "A rationale is required.",
    "listing_suspended": "This listing is suspended, so its ownership cannot change.",
    "transfer_unconfirmed": (
        "This listing already has an owner. Confirm the ownership transfer to "
        "approve a dispute."
    ),
    "item_not_open": "This item has already been decided.",
    "wrong_type": "This action does not apply to this kind of item.",
}


def refusal_text(reason: str) -> str:
    return REFUSAL_TEXT.get(reason, "This action was refused.")


# --- shared primitives (also used by directory.services) ---------------------


def expire_pending_code(claim: Claim, *, now) -> None:
    """Kill the claim's code message if it is still pending, so a dead code is
    never sent after an outage (decisions.md §4.4: a resend supersedes the
    prior code). Shared by the resend supersede and every flip or decision
    that retires a code."""
    if claim.code_message_id is None:
        return
    prior = claim.code_message
    if prior.status == OutboundMessage.Status.PENDING:
        prior.expires_at = now
        prior.save(update_fields=["expires_at"])


def lock_guard(tenant, *, listing, claimant) -> ClaimVerificationGuard:
    guard, _ = ClaimVerificationGuard.objects.get_or_create(
        tenant=tenant, listing=listing, claimant=claimant
    )
    return ClaimVerificationGuard.objects.select_for_update().get(pk=guard.pk)


def _claim_ref(claim: Claim) -> dict:
    return {
        "id": claim.public_id,
        "listing_id": claim.listing.public_id,
        "status": claim.status,
        "method": claim.method,
    }


def open_item(
    tenant,
    *,
    item_type: str,
    listing,
    claimant,
    claim,
    rules: list,
    actor: dict,
) -> ModerationItem:
    """The open item for this dispute, lock or block, creating it -- and
    emitting ``moderation.queued`` -- only if none is open. Runs in the
    causing command's transaction."""
    existing = ModerationItem.objects.filter(
        item_type=item_type, status=ModerationItem.Status.OPEN
    )
    if item_type == ModerationItem.ItemType.CLAIM_DISPUTE:
        existing = existing.filter(claim=claim)
    else:
        existing = existing.filter(listing=listing, claimant=claimant)
    item = existing.first()
    if item is not None:
        return item
    item = ModerationItem.objects.create(
        tenant=tenant,
        item_type=item_type,
        listing=listing,
        claimant=claimant,
        claim=claim,
        rules_triggered=rules,
        priority="normal",
    )
    emit(
        events.MODERATION_QUEUED,
        subject=item.public_id,
        tenant=tenant,
        actor=actor,
        data={
            "item_type": item.item_type,
            "item_id": item.public_id,
            "claim_id": claim.public_id if claim is not None else None,
            "listing_id": listing.public_id,
            "rules_triggered": rules,
            "priority": item.priority,
        },
    )
    return item


def _decide_item(
    tenant, item: ModerationItem, *, decision: str, operator, rationale: str,
    actor: dict, now,
):
    item.status = ModerationItem.Status.DECIDED
    item.decision = decision
    item.decided_by = operator
    item.decided_at = now
    item.rationale = rationale
    item.save(update_fields=["status", "decision", "decided_by", "decided_at", "rationale"])
    return emit(
        events.MODERATION_DECIDED,
        subject=item.public_id,
        tenant=tenant,
        actor=actor,
        data={
            "item_type": item.item_type,
            "item_id": item.public_id,
            "claim_id": item.claim.public_id if item.claim_id else None,
            "decision": decision,
            "decided_by": "human",
            "rationale": rationale,
        },
    )


def _decide_claim_item(tenant, claim: Claim, *, decision, operator, rationale, actor, now):
    item = ModerationItem.objects.filter(
        claim=claim,
        item_type=ModerationItem.ItemType.CLAIM_DISPUTE,
        status=ModerationItem.Status.OPEN,
    ).first()
    if item is not None:
        _decide_item(
            tenant, item, decision=decision, operator=operator,
            rationale=rationale, actor=actor, now=now,
        )


def dispute(tenant, *, claim: Claim, actor: dict):
    """Move ``claim`` to ``disputed``: ``claim.disputed`` then a moderation
    item (spec §9.4). Returns the ``claim.disputed`` event. Used by submit on a
    claimed listing and, for a listing that became owned between submit and
    verify or approve, by the paths that would otherwise have approved."""
    claim.status = Claim.Status.DISPUTED
    claim.review_reason = "disputed"  # the most useful thing to tell a reviewer
    claim.save(update_fields=["status", "review_reason"])
    event = emit(
        events.CLAIM_DISPUTED,
        subject=claim.public_id,
        tenant=tenant,
        actor=actor,
        data={"claim": {"id": claim.public_id, "listing_id": claim.listing.public_id}},
    )
    open_item(
        tenant,
        item_type=ModerationItem.ItemType.CLAIM_DISPUTE,
        listing=claim.listing,
        claimant=claim.claimant,
        claim=claim,
        rules=["duplicate_claim"],
        actor=actor,
    )
    notify_operators(tenant)
    return event


# --- the one owner writer ----------------------------------------------------


def _assign_owner(listing: Listing, user) -> None:
    """The only write to ``Listing.owner`` and ``Listing.status`` in the
    codebase (#48). ``owner`` is the fact; ``status`` mirrors it."""
    listing.owner = user
    listing.status = Listing.Status.CLAIMED
    listing.save(update_fields=["owner", "status"])


# --- the review queue's queries ----------------------------------------------


def review_claims():
    """Claims waiting on a human: chosen or flipped to manual, or a verified
    code that could not move ownership (a suspended listing)."""
    return (
        Claim.objects.filter(status=Claim.Status.PENDING_VERIFICATION)
        .filter(Q(method=Claim.Method.MANUAL) | Q(verified_at__isnull=False))
        .select_related("listing", "claimant")
        .order_by("created_at")
    )


def open_items(*item_types):
    return (
        ModerationItem.objects.filter(
            status=ModerationItem.Status.OPEN, item_type__in=item_types
        )
        .select_related("listing", "claimant", "claim")
        .order_by("created_at")
    )


def dispute_items():
    return open_items(ModerationItem.ItemType.CLAIM_DISPUTE)


def lock_items():
    return open_items(
        ModerationItem.ItemType.VERIFICATION_LOCK,
        ModerationItem.ItemType.CLAIM_BLOCK,
    )


def open_count() -> int:
    """Everything waiting on a human, for the admin home page."""
    return review_claims().count() + dispute_items().count() + lock_items().count()


def waiting_claims_count() -> int:
    """Claims waiting for a human, for the operator notice: those in manual
    review plus open disputes. A lock's claim is already a manual-review claim
    and a block's claim is rejected, so neither adds to the figure."""
    return review_claims().count() + dispute_items().count()


def _operator_recipients(tenant) -> "list[str]":
    """Active memberships at ``DECIDE_ROLE`` and above, as lowercased addresses."""
    recipients: list[str] = []
    for membership in (
        StaffMembership.objects.filter(
            tenant=tenant,
            status=StaffMembership.Status.ACTIVE,
            role__gte=DECIDE_ROLE,
            operator__is_active=True,
        )
        .select_related("operator")
        .order_by("id")
    ):
        address = (membership.operator.email or "").strip().lower()
        if address and address not in recipients:
            recipients.append(address)
    return recipients


def _send_operator_notice(tenant, addresses, *, now) -> int:
    """Queue the count-and-link notice to each address. Carries no
    claimant-supplied text."""
    waiting = waiting_claims_count()
    noun = "claim is" if waiting == 1 else "claims are"
    url = _link(tenant, "/admin/claims/")
    link_line = f"\n\nReview them at: {url}" if url else ""
    body = (
        f"{waiting} {noun} waiting for review on {tenant.name}."
        f"{link_line}"
        f"\n\nYou get at most one of these every 15 minutes, so the number "
        f"may be higher by the time you open the queue."
    )
    for address in addresses:
        enqueue(
            tenant=tenant,
            kind=OPERATOR_NOTICE_KIND,
            to_address=address,
            subject=f"Claims waiting for review on {tenant.name}",
            body_text=body,
            expires_at=now + OPERATOR_NOTICE_TTL,
        )
    return len(addresses)


def notify_operators(tenant, *, now=None) -> int:
    """Tell the staff who can act that a claim has entered manual review
    (#218; spec §9.6 "with the operator notified"). Called from the
    transaction that put the claim there -- chosen manual, any flip to manual,
    or a dispute -- so the message commits with the state change. Returns the
    number of messages queued.

    Recipients are active memberships at ``DECIDE_ROLE`` and above: the people
    who can approve or reject. A recipient who was sent one in the last
    ``OPERATOR_NOTICE_THROTTLE`` is skipped -- a leading-edge throttle derived
    from recent ``OutboundMessage`` rows, with no table of its own -- and
    ``flush_operator_notices`` (the worker's ``claim_notice_flush`` job)
    announces what the skip deferred once the window has passed (#241).
    Claim submission is unauthenticated, so without the throttle anyone could
    flood every editor.

    The message carries a count and, only when the tenant has an absolute
    base, a link to the queue. It carries no claimant-supplied text.
    """
    now = now or timezone.now()
    recipients = _operator_recipients(tenant)
    if not recipients:
        return 0

    recent = set(
        OutboundMessage.all_tenants.filter(
            tenant=tenant,
            kind=OPERATOR_NOTICE_KIND,
            to_address__in=recipients,
            created_at__gt=now - OPERATOR_NOTICE_THROTTLE,
        ).values_list("to_address", flat=True)
    )
    due = [address for address in recipients if address not in recent]
    if not due:
        return 0
    return _send_operator_notice(tenant, due, now=now)


def _entered_review_since(since) -> bool:
    """Did a claim enter review, or a dispute open, after ``since``? A claim in
    review is a pending one and nothing else saves it, so ``updated_at`` is
    when it flipped; a dispute is its item's ``created_at``."""
    return (
        review_claims().filter(updated_at__gt=since).exists()
        or dispute_items().filter(created_at__gt=since).exists()
    )


def flush_operator_notices(tenant, *, now) -> int:
    """Trailing flush for the operator notice (#241). Call with the tenant in
    scope and inside a transaction (``enqueue`` requires one).

    A recipient is told when their last notice is past the throttle window and
    a claim entered review after it (or, with no notice on record, inside the
    notice lifetime). The notice itself resets the clock, so the same arrival
    is never announced twice, and the inline path, which reads the same rows,
    is throttled by it too. Returns the number of messages queued.
    """
    recipients = _operator_recipients(tenant)
    if not recipients:
        return 0
    lasts = {
        address: created
        for address, created in OutboundMessage.all_tenants.filter(
            tenant=tenant, kind=OPERATOR_NOTICE_KIND, to_address__in=recipients
        )
        .order_by("created_at")
        .values_list("to_address", "created_at")
    }
    floor = now - OPERATOR_NOTICE_TTL
    due = []
    for address in recipients:
        last = lasts.get(address)
        if last is not None and last > now - OPERATOR_NOTICE_THROTTLE:
            continue
        if _entered_review_since(max(last, floor) if last else floor):
            due.append(address)
    if not due:
        return 0
    return _send_operator_notice(tenant, due, now=now)


def _link(tenant, path: str) -> "str | None":
    if routing.has_absolute_base(tenant):
        return routing.absolute_url(tenant, path)
    return None


def _notify_existing_contacts(tenant, *, claim, listing, previous_owner, now) -> list:
    """The anti-hijack notice (spec §9.4): email to whatever contact the
    listing already had and, on a transfer, to the owner it had. Email only --
    the project has no ``sms.send`` capability. Returns the channels queued,
    destinations masked. The message carries no claimant-supplied text."""
    addresses: list[str] = []
    for raw in (listing.email, previous_owner.email if previous_owner else ""):
        address = (raw or "").strip().lower()
        if address and address != claim.claimant.email and address not in addresses:
            addresses.append(address)
    if not addresses:
        return []
    url = _link(tenant, f"/claim/{listing.public_id}/")
    link_line = (
        f"\n\nIf this was not you, the genuine owner can dispute the claim by "
        f"submitting one at: {url}"
        if url
        else "\n\nIf this was not you, contact the directory to dispute the claim."
    )
    body = (
        f"The listing {listing.name} on {tenant.name} was just claimed by "
        f"someone else. You are receiving this because this address is already "
        f"on the listing.\n\nIf you or someone you authorised did this, no "
        f"action is needed."
        f"{link_line}"
    )
    channels = []
    for address in addresses:
        enqueue(
            tenant=tenant,
            kind="claim.existing_contact_notice",
            to_address=address,
            subject=f"Your listing {listing.name} was just claimed",
            body_text=body,
            expires_at=now + ANTI_HIJACK_NOTICE_TTL,
        )
        channels.append({"channel": "email", "destination": mask_email(address)})
    return channels


def _reject_losers(tenant, *, winner: Claim, listing, now) -> None:
    """Approving one claim disposes of the other pending claims on the listing
    (ruling 2026-09-11): rejected by the system, and not counted toward the
    three-rejection block -- that is no judgement on the claimant. Disputed
    claims are not losers; they stay in the queue.

    The losers are locked *after* the listing, while ``claim.verify`` locks a
    claim and then the listing -- the opposite order -- so a plain
    ``FOR UPDATE`` here could deadlock against a correct code being entered
    at the same moment. ``skip_locked`` makes that impossible: a claim another
    transaction holds is left alone, and stays pending on a listing that is
    now claimed. Its own ``claim.verify`` then finds the listing claimed and
    disputes (``resolve_verified_claim``), or an operator's approval does the
    same, which is the correct outcome for it."""
    actor = {"type": "system", "id": "claim.approve"}
    losers = Claim.objects.select_for_update(skip_locked=True).filter(
        listing=listing, status=Claim.Status.PENDING_VERIFICATION
    ).exclude(pk=winner.pk)
    for loser in losers:
        loser.status = Claim.Status.REJECTED
        loser.decided_at = now
        loser.rejection_reason = LOSER_REASON
        loser.code_hash = ""
        expire_pending_code(loser, now=now)
        loser.save(
            update_fields=["status", "decided_at", "rejection_reason", "code_hash"]
        )
        emit(
            events.CLAIM_REJECTED,
            subject=loser.public_id,
            tenant=tenant,
            actor=actor,
            data={
                "claim": _claim_ref(loser),
                "reason": LOSER_REASON,
                "decided_by": "system",
            },
        )
        # Told like any rejected claimant, same producer and deadline; the
        # closing line differs, since more information will not change this.
        _mail_rejection(
            tenant, claim=loser, reason=LOSER_REASON, blocked=False, now=now,
            lost_to_other=True,
        )


def _approve_claim(
    tenant, *, claim: Claim, listing: Listing, actor: dict, now,
    operator=None, manual: "dict | None" = None,
) -> str:
    """Approve ``claim`` and make its claimant the owner. Runs in the caller's
    transaction with the claim and listing rows already locked. ``manual`` is
    ``{"method_used", "notes", "evidence_ref"}`` for an operator approval and
    ``None`` for one earned by a verified code. Returns the
    ``claim.approved`` event id."""
    previous_owner = listing.owner if listing.owner_id else None

    # A transfer retires the old approved claim first, so the partial unique
    # "one approved claim per listing" holds at every instant.
    for old in Claim.objects.filter(
        listing=listing, status=Claim.Status.APPROVED
    ).exclude(pk=claim.pk):
        old.status = Claim.Status.SUPERSEDED
        old.save(update_fields=["status"])

    claim.status = Claim.Status.APPROVED
    claim.decided_at = now
    claim.code_hash = ""
    fields = ["status", "decided_at", "code_hash"]
    if manual is not None:
        claim.method = Claim.Method.MANUAL
        claim.manual_method_used = manual["method_used"]
        claim.verification_notes = manual["notes"]
        claim.evidence_ref = manual["evidence_ref"]
        claim.verified_by = operator
        claim.verified_at = now
        claim.decided_by = operator
        fields += [
            "method", "manual_method_used", "verification_notes",
            "evidence_ref", "verified_by", "verified_at", "decided_by",
        ]
    expire_pending_code(claim, now=now)
    claim.save(update_fields=fields)

    _assign_owner(listing, claim.claimant)

    channels = _notify_existing_contacts(
        tenant, claim=claim, listing=listing, previous_owner=previous_owner, now=now
    )

    data = {
        "claim": {
            "id": claim.public_id,
            "listing_id": listing.public_id,
            "method": claim.method,
        },
        "transfer": previous_owner is not None,
        "existing_contacts_notified": bool(channels),
    }
    if manual is not None:
        data["manual_verification"] = {
            "method_used": manual["method_used"],
            "verified_by": operator.public_id,
            "verified_at": now.isoformat(),
            "notes": manual["notes"],
            "evidence_ref": manual["evidence_ref"] or None,
        }
    else:
        data["verification"] = {
            "method": claim.method,
            "verified_at": claim.verified_at.isoformat() if claim.verified_at else None,
        }
    approved = emit(
        events.CLAIM_APPROVED, subject=claim.public_id, tenant=tenant,
        actor=actor, data=data,
    )
    emit(
        events.LISTING_OWNER_ASSIGNED,
        subject=listing.public_id,
        tenant=tenant,
        actor=actor,
        data={
            "listing_id": listing.public_id,
            "owner": {"id": claim.claimant.public_id},
            "claim_id": claim.public_id,
            "previous_owner_id": previous_owner.public_id if previous_owner else None,
        },
    )
    emit(
        events.LISTING_UPDATED,
        subject=listing.public_id,
        tenant=tenant,
        actor=actor,
        data={
            "changes": [
                {"op": "replace", "path": "/status", "value": listing.status},
                {
                    "op": "replace" if previous_owner else "add",
                    "path": "/owner_id",
                    "value": claim.claimant.public_id,
                },
            ],
            "type": listing.listing_type.key,
        },
    )
    if channels:
        emit(
            events.CLAIM_NOTIFIED_EXISTING_CONTACTS,
            subject=claim.public_id,
            tenant=tenant,
            actor=actor,
            data={
                "claim_id": claim.public_id,
                "listing_id": listing.public_id,
                "channels": channels,
            },
        )

    _reject_losers(tenant, winner=claim, listing=listing, now=now)
    if manual is not None:
        _decide_claim_item(
            tenant, claim, decision="approved", operator=operator,
            rationale=manual["notes"], actor=actor, now=now,
        )
    return approved.event_id


def resolve_verified_claim(tenant, *, claim: Claim, now) -> "str | None":
    """A correct code has just set ``verified_at`` (decisions.md §4.4, §4.6).
    Called inside ``claim.verify``'s transaction with the claim locked.

    * unowned listing: approve, in this transaction;
    * owned listing -- including one that became owned since submit: dispute;
    * suspended listing: nothing moves; the claim stays open for review.

    Returns the event id the command concludes with, or ``None``."""
    listing = Listing.objects.select_for_update().get(pk=claim.listing_id)
    actor = {"type": "visitor", "id": claim.claimant.public_id}
    if listing.status == Listing.Status.SUSPENDED:
        return None
    if listing.status == Listing.Status.CLAIMED:
        return dispute(tenant, claim=claim, actor=actor).event_id
    return _approve_claim(tenant, claim=claim, listing=listing, actor=actor, now=now)


# --- operator commands -------------------------------------------------------


def _membership(tenant, operator) -> "StaffMembership | None":
    return StaffMembership.objects.filter(
        operator=operator, tenant=tenant, status=StaffMembership.Status.ACTIVE
    ).first()


def _operator_actor(operator, membership) -> dict:
    """``admin`` for an active admin membership, ``staff`` below it (spec
    §4.4's reading of ``actor.type``)."""
    kind = "admin" if membership and membership.role >= StaffMembership.Role.ADMIN else "staff"
    return {"type": kind, "id": operator.public_id}


def _require(membership, role) -> None:
    if membership is None or membership.role < role:
        raise ReviewRefused("forbidden")


def _conclude_refused(row, exc: ReviewRefused) -> None:
    log_conclude(
        row,
        outcome="blocked" if exc.reason == "forbidden" else "rejected",
        problem={"reason": exc.reason},
    )


@dataclass
class ApproveResult:
    claim: Claim
    outcome: str  # "approved" | "disputed"


def approve_claim(
    tenant, *, claim: Claim, operator, method_used: str, notes: str,
    evidence_ref: str = "", transfer: bool = False,
) -> ApproveResult:
    """The ``claim.approve`` command. ``notes`` are required: an admin who
    cannot say how they verified something has not verified it (spec §9.3).
    Approving a pending claim on a listing that has since been claimed moves
    it to ``disputed`` instead and concludes ``applied``; approving a
    disputed claim on an owned listing needs ``transfer=True``."""
    require_autocommit()
    membership = _membership(tenant, operator)
    actor = _operator_actor(operator, membership)
    row = log_received(
        command="claim.approve", tenant=tenant, idempotency_key=None,
        actor=actor, trace_id=None, origin="",
        payload={"claim_id": claim.public_id, "manual_method_used": method_used},
    )
    try:
        claim, outcome, event_id = _apply_approve(
            tenant, claim=claim, operator=operator, membership=membership,
            actor=actor, method_used=method_used, notes=notes,
            evidence_ref=evidence_ref, transfer=transfer,
        )
    except ReviewRefused as exc:
        _conclude_refused(row, exc)
        raise
    log_conclude(row, outcome="applied", result_event_id=event_id)
    return ApproveResult(claim, outcome)


@transaction.atomic
def _apply_approve(
    tenant, *, claim, operator, membership, actor, method_used, notes,
    evidence_ref, transfer,
):
    _require(membership, DECIDE_ROLE)
    notes = (notes or "").strip()
    if not notes:
        raise ReviewRefused("notes_required")
    if method_used not in Claim.ManualMethod.values:
        raise ReviewRefused("method_used_invalid")

    claim = Claim.objects.select_for_update().get(pk=claim.pk)
    if claim.status not in OPEN_CLAIM_STATUSES:
        raise ReviewRefused("status")
    listing = Listing.objects.select_for_update().get(pk=claim.listing_id)
    if listing.status == Listing.Status.SUSPENDED:
        raise ReviewRefused("listing_suspended")

    now = timezone.now()
    owned = listing.status == Listing.Status.CLAIMED
    if claim.status == Claim.Status.PENDING_VERIFICATION and owned:
        event = dispute(tenant, claim=claim, actor=actor)
        return claim, "disputed", event.event_id
    if owned and not transfer:
        raise ReviewRefused("transfer_unconfirmed")

    event_id = _approve_claim(
        tenant, claim=claim, listing=listing, actor=actor, now=now,
        operator=operator,
        manual={
            "method_used": method_used,
            "notes": notes,
            "evidence_ref": (evidence_ref or "").strip(),
        },
    )
    return claim, "approved", event_id


def reject_claim(tenant, *, claim: Claim, operator, reason: str) -> Claim:
    """The ``claim.reject`` command. The reason is required and mailed to the
    claimant: a rejected claimant may submit again, and the reason is what
    tells them what was missing (spec §9.6). The third rejection on a
    ``(listing, claimant)`` pair blocks it and opens a ``claim_block`` item."""
    require_autocommit()
    membership = _membership(tenant, operator)
    actor = _operator_actor(operator, membership)
    row = log_received(
        command="claim.reject", tenant=tenant, idempotency_key=None,
        actor=actor, trace_id=None, origin="",
        payload={"claim_id": claim.public_id},
    )
    try:
        claim, event_id = _apply_reject(
            tenant, claim=claim, operator=operator, membership=membership,
            actor=actor, reason=reason,
        )
    except ReviewRefused as exc:
        _conclude_refused(row, exc)
        raise
    log_conclude(row, outcome="applied", result_event_id=event_id)
    return claim


@transaction.atomic
def _apply_reject(tenant, *, claim, operator, membership, actor, reason):
    _require(membership, DECIDE_ROLE)
    reason = (reason or "").strip()
    if not reason:
        raise ReviewRefused("reason_required")

    guard = lock_guard(tenant, listing=claim.listing, claimant=claim.claimant)
    claim = Claim.objects.select_for_update().get(pk=claim.pk)
    if claim.status not in OPEN_CLAIM_STATUSES:
        raise ReviewRefused("status")

    now = timezone.now()
    claim.status = Claim.Status.REJECTED
    claim.decided_at = now
    claim.decided_by = operator
    claim.rejection_reason = reason
    claim.code_hash = ""
    expire_pending_code(claim, now=now)
    claim.save(
        update_fields=[
            "status", "decided_at", "decided_by", "rejection_reason", "code_hash",
        ]
    )

    # Only the rejection counter and the block move here. The verification
    # counters are a separate brute-force defence: a rejection that reset one
    # would be a route around it (spec §9.6).
    guard.rejections += 1
    blocked = False
    if guard.rejections >= REJECTION_BLOCK_THRESHOLD and guard.claim_blocked_at is None:
        guard.claim_blocked_at = now
        blocked = True
    guard.save(update_fields=["rejections", "claim_blocked_at"])

    event = emit(
        events.CLAIM_REJECTED,
        subject=claim.public_id,
        tenant=tenant,
        actor=actor,
        data={
            "claim": _claim_ref(claim),
            "reason": reason,
            "decided_by": operator.public_id,
        },
    )
    _decide_claim_item(
        tenant, claim, decision="rejected", operator=operator,
        rationale=reason, actor=actor, now=now,
    )
    if blocked:
        open_item(
            tenant,
            item_type=ModerationItem.ItemType.CLAIM_BLOCK,
            listing=claim.listing,
            claimant=claim.claimant,
            claim=claim,
            rules=["repeated_rejection"],
            actor=actor,
        )
    _mail_rejection(tenant, claim=claim, reason=reason, blocked=blocked, now=now)
    return claim, event.event_id


def _mail_rejection(
    tenant, *, claim, reason, blocked, now, lost_to_other: bool = False
) -> None:
    url = _link(tenant, f"/claim/{claim.listing.public_id}/")
    if lost_to_other:
        next_line = (
            f"\n\nIf you believe the listing is yours, you can dispute the "
            f"approved claim by submitting a new one at: {url}"
            if url
            else "\n\nIf you believe the listing is yours, contact the directory."
        )
    elif blocked:
        next_line = (
            "\n\nAfter repeated rejections this address can no longer claim "
            "this listing. Contact the directory if you think this is a mistake."
        )
    elif url:
        next_line = f"\n\nYou may submit a new claim with more information at: {url}"
    else:
        next_line = "\n\nYou may submit a new claim with more information."
    enqueue(
        tenant=tenant,
        kind="claim.rejected",
        to_address=claim.claimant.email,
        subject=f"Update on your claim for {claim.listing.name}",
        body_text=(
            f"Your claim for {claim.listing.name} on {tenant.name} was not "
            f"approved.\n\nThe reviewer wrote:\n{reason}{next_line}"
        ),
        expires_at=now + REJECTION_NOTICE_TTL,
    )


def unlock_verification(tenant, *, item: ModerationItem, operator, rationale: str) -> ModerationItem:
    """The ``claim.unlock_verification`` command: only an operator clears a
    verification lock (spec §9.6). Clears the lock and resets the verification
    counters -- without that reset the next cooldown would re-lock the pair
    at once -- and never touches ``rejections`` or ``claim_blocked_at``."""
    return _queue_command(
        tenant, item=item, operator=operator, rationale=rationale,
        command="claim.unlock_verification",
        item_type=ModerationItem.ItemType.VERIFICATION_LOCK,
        decision="unlocked",
    )


def unblock_claims(tenant, *, item: ModerationItem, operator, rationale: str) -> ModerationItem:
    """The ``claim.unblock`` command: clears ``claim_blocked_at`` and nothing
    else. ``rejections`` stays at its count, so the next rejection re-blocks
    the pair at once -- the block is lifted, the history is not."""
    return _queue_command(
        tenant, item=item, operator=operator, rationale=rationale,
        command="claim.unblock",
        item_type=ModerationItem.ItemType.CLAIM_BLOCK,
        decision="unblocked",
    )


def _queue_command(tenant, *, item, operator, rationale, command, item_type, decision):
    require_autocommit()
    membership = _membership(tenant, operator)
    actor = _operator_actor(operator, membership)
    row = log_received(
        command=command, tenant=tenant, idempotency_key=None, actor=actor,
        trace_id=None, origin="", payload={"item_id": item.public_id},
    )
    try:
        item, event_id = _apply_queue_command(
            tenant, item=item, operator=operator, membership=membership,
            actor=actor, rationale=rationale, item_type=item_type,
            decision=decision,
        )
    except ReviewRefused as exc:
        _conclude_refused(row, exc)
        raise
    log_conclude(row, outcome="applied", result_event_id=event_id)
    return item


@transaction.atomic
def _apply_queue_command(
    tenant, *, item, operator, membership, actor, rationale, item_type, decision
):
    _require(membership, QUEUE_ROLE)
    rationale = (rationale or "").strip()
    if not rationale:
        raise ReviewRefused("rationale_required")
    if item.item_type != item_type:
        raise ReviewRefused("wrong_type")

    guard = lock_guard(tenant, listing=item.listing, claimant=item.claimant)
    item = ModerationItem.objects.select_for_update().get(pk=item.pk)
    if item.status != ModerationItem.Status.OPEN:
        raise ReviewRefused("item_not_open")

    now = timezone.now()
    # An item must never be un-closable: if the state it describes is already
    # cleared (by direct SQL, or a path this PR does not know of), the command
    # still succeeds, clears nothing, and decides the item ``already_cleared``.
    if item_type == ModerationItem.ItemType.VERIFICATION_LOCK:
        if guard.verification_locked_at is None:
            decision = ALREADY_CLEARED
        else:
            guard.verification_locked_at = None
            guard.cooldowns = 0
            guard.wrong_entries = 0
            guard.cooldown_until = None
            guard.save(
                update_fields=[
                    "verification_locked_at", "cooldowns", "wrong_entries",
                    "cooldown_until",
                ]
            )
    else:
        if guard.claim_blocked_at is None:
            decision = ALREADY_CLEARED
        else:
            guard.claim_blocked_at = None
            guard.save(update_fields=["claim_blocked_at"])

    event = _decide_item(
        tenant, item, decision=decision, operator=operator,
        rationale=rationale, actor=actor, now=now,
    )
    return item, event.event_id
