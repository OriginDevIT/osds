"""The claims review queue (claims PR 4a -- decisions.md §4.6).

Reading the queue and a claim is moderator work; approving and rejecting is
the editor's, and unlocking or unblocking an address is a moderation-queue
action at moderator (spec §4.4). A moderator sees a claimant's contact details
masked and no consent record -- personal data starts at the editor cut. Every
POST route calls exactly one command in ``directory.claim_review``, which
checks the role again: the decorator is the page gate, the service is the
authority.
"""

from __future__ import annotations

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from audit.models import OutboxEvent
from directory import claim_review
from directory.access import tenant_admin_required
from directory.masking import mask_email, mask_phone_e164
from directory.models import Claim, ClaimVerificationGuard, Consent, ModerationItem
from tenants.models import StaffMembership

_VIEW = tenant_admin_required(claim_review.QUEUE_ROLE)
_DECIDE = tenant_admin_required(claim_review.DECIDE_ROLE)
_QUEUE = tenant_admin_required(claim_review.QUEUE_ROLE)

TABS = ("review", "disputes", "locks")


def _review_claims():
    """Claims waiting on a human: chosen or flipped to manual, or a verified
    code that could not move ownership (a suspended listing)."""
    return (
        Claim.objects.filter(status=Claim.Status.PENDING_VERIFICATION)
        .filter(Q(method=Claim.Method.MANUAL) | Q(verified_at__isnull=False))
        .select_related("listing", "claimant")
        .order_by("created_at")
    )


def _open_items(*item_types):
    return (
        ModerationItem.objects.filter(
            status=ModerationItem.Status.OPEN, item_type__in=item_types
        )
        .select_related("listing", "claimant", "claim")
        .order_by("created_at")
    )


def _disputes():
    return _open_items(ModerationItem.ItemType.CLAIM_DISPUTE)


def _locks():
    return _open_items(
        ModerationItem.ItemType.VERIFICATION_LOCK,
        ModerationItem.ItemType.CLAIM_BLOCK,
    )


def open_count() -> int:
    """Everything waiting on a human, for the admin home page."""
    return _review_claims().count() + _disputes().count() + _locks().count()


@_QUEUE
def claims_queue(request):
    tab = request.GET.get("tab")
    if tab not in TABS:
        tab = "review"
    rows = {"review": _review_claims, "disputes": _disputes, "locks": _locks}[tab]()
    return render(
        request,
        "directory/admin/claims_queue.html",
        {
            "tab": tab,
            "rows": rows,
            "counts": {
                "review": _review_claims().count(),
                "disputes": _disputes().count(),
                "locks": _locks().count(),
            },
        },
    )


def _can_decide(request) -> bool:
    return request.membership.role >= claim_review.DECIDE_ROLE


@_VIEW
def claim_detail(request, public_id):
    claim = get_object_or_404(
        Claim.objects.select_related("listing", "claimant", "listing__owner"),
        public_id=public_id,
    )
    can_decide = _can_decide(request)
    guard = ClaimVerificationGuard.objects.filter(
        listing=claim.listing, claimant=claim.claimant
    ).first()
    claimant = claim.claimant
    timeline = OutboxEvent.all_tenants.filter(
        tenant=request.tenant, subject=claim.public_id
    ).order_by("id")
    return render(
        request,
        "directory/admin/claim_detail.html",
        {
            "claim": claim,
            "listing": claim.listing,
            "owner": claim.listing.owner,
            "claimant": claimant,
            "claimant_email": claimant.email if can_decide else mask_email(claimant.email),
            "claimant_phone": (
                claimant.phone_e164
                if can_decide
                else (mask_phone_e164(claimant.phone_e164) if claimant.phone_e164 else "")
            ),
            "consents": Consent.objects.filter(claim=claim).order_by("id") if can_decide else [],
            "guard": guard,
            "timeline": timeline,
            "can_decide": can_decide,
            "is_open": claim.status in claim_review.OPEN_CLAIM_STATUSES,
            "transfer_needed": (
                claim.status == Claim.Status.DISPUTED
                and claim.listing.status == "claimed"
            ),
            "manual_methods": Claim.ManualMethod.choices,
            "item": ModerationItem.objects.filter(claim=claim).order_by("-id").first(),
        },
    )


@_QUEUE
def moderation_detail(request, public_id):
    item = get_object_or_404(
        ModerationItem.objects.select_related("listing", "claimant", "claim"),
        public_id=public_id,
    )
    guard = ClaimVerificationGuard.objects.filter(
        listing=item.listing, claimant=item.claimant
    ).first()
    can_see_contact = request.membership.role >= claim_review.DECIDE_ROLE
    return render(
        request,
        "directory/admin/moderation_detail.html",
        {
            "item": item,
            "guard": guard,
            "claimant_email": (
                item.claimant.email if can_see_contact else mask_email(item.claimant.email)
            ),
            "is_open": item.status == ModerationItem.Status.OPEN,
        },
    )


def _refused(request, exc: claim_review.ReviewRefused) -> None:
    messages.error(request, claim_review.refusal_text(exc.reason))


@_DECIDE
@require_POST
def claim_approve(request, public_id):
    claim = get_object_or_404(Claim.objects, public_id=public_id)
    try:
        result = claim_review.approve_claim(
            request.tenant,
            claim=claim,
            operator=request.user,
            method_used=request.POST.get("method_used", ""),
            notes=request.POST.get("notes", ""),
            evidence_ref=request.POST.get("evidence_ref", ""),
            transfer=request.POST.get("confirm_transfer") == "1",
        )
    except claim_review.ReviewRefused as exc:
        _refused(request, exc)
    else:
        if result.outcome == "disputed":
            messages.warning(
                request,
                "This listing now has an owner, so the claim was moved to disputes. "
                "Nothing was approved.",
            )
        else:
            messages.success(request, "Claim approved. The claimant now owns the listing.")
    return redirect("directory_admin:claim-detail", public_id=public_id)


@_DECIDE
@require_POST
def claim_reject(request, public_id):
    claim = get_object_or_404(Claim.objects, public_id=public_id)
    try:
        claim_review.reject_claim(
            request.tenant,
            claim=claim,
            operator=request.user,
            reason=request.POST.get("reason", ""),
        )
    except claim_review.ReviewRefused as exc:
        _refused(request, exc)
    else:
        messages.success(request, "Claim rejected. The claimant has been told why.")
    return redirect("directory_admin:claim-detail", public_id=public_id)


def _queue_action(request, public_id, command, success):
    item = get_object_or_404(ModerationItem.objects, public_id=public_id)
    try:
        command(
            request.tenant,
            item=item,
            operator=request.user,
            rationale=request.POST.get("rationale", ""),
        )
    except claim_review.ReviewRefused as exc:
        _refused(request, exc)
    else:
        messages.success(request, success)
    return redirect("directory_admin:moderation-detail", public_id=public_id)


@_QUEUE
@require_POST
def moderation_unlock(request, public_id):
    return _queue_action(
        request, public_id, claim_review.unlock_verification,
        "Verification unlocked for this address on this listing.",
    )


@_QUEUE
@require_POST
def moderation_unblock(request, public_id):
    return _queue_action(
        request, public_id, claim_review.unblock_claims,
        "This address can claim this listing again.",
    )
