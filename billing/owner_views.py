"""The owner's billing routes under ``/owner/listings/<id>/billing/``
(decisions.md §4.11): start a checkout, cancel, open the provider's card page,
and the page the provider sends the owner back to.

Ownership is resolved live on every request, as on the rest of the owner's
pages: a listing that is not the owner's is a 404. Every action is its own
route calling one command, which checks ownership again.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from audit.ratelimit import RateLimited
from billing import payments
from directory import routing
from directory.models import Listing
from directory.owner_auth import owner_required


def _owned(request, public_id) -> Listing:
    return get_object_or_404(
        Listing.objects.filter(owner=request.owner).select_related("listing_type"),
        public_id=public_id,
    )


def _back(public_id):
    return redirect("owner-listing", public_id=public_id)


@owner_required
@require_POST
def checkout(request, public_id):
    listing = _owned(request, public_id)
    try:
        url = payments.start_checkout(
            request.tenant, listing=listing, user=request.owner,
            tier_key=request.POST.get("tier", ""),
        )
    except RateLimited:
        messages.error(request, "Too many attempts. Please try again later.")
    except payments.PaymentRefused as exc:
        messages.error(request, payments.refusal_text(exc.reason))
    else:
        return HttpResponseRedirect(url)  # the provider's page, off this site
    return _back(public_id)


@owner_required
@require_POST
def cancel(request, public_id):
    listing = _owned(request, public_id)
    try:
        payments.cancel_entitlement(
            request.tenant, listing=listing, by="owner", user=request.owner,
            reason=request.POST.get("reason", ""),
        )
    except payments.PaymentRefused as exc:
        messages.error(request, payments.refusal_text(exc.reason))
    else:
        messages.success(request, "Your plan is cancelled.")
    return _back(public_id)


@never_cache
@owner_required
@require_GET
def portal(request, public_id):
    listing = _owned(request, public_id)
    back = routing.absolute_url(request.tenant, f"/owner/listings/{listing.public_id}/")
    try:
        url = payments.portal_url(
            request.tenant, listing=listing, user=request.owner, return_url=back
        )
    except payments.PaymentRefused as exc:
        messages.error(request, payments.refusal_text(exc.reason))
        return _back(public_id)
    if not url:
        messages.error(request, payments.refusal_text("no_portal"))
        return _back(public_id)
    return HttpResponseRedirect(url)


@never_cache
@owner_required
@require_GET
def return_page(request, public_id):
    """Where the provider sends the owner after checkout. It states nothing the
    owner could forge into a fact: the entitlement changes only when the
    provider's own report arrives, so this page says it may take a moment."""
    listing = _owned(request, public_id)
    return render(
        request, "billing/owner/return.html",
        {"listing": listing, "outcome": request.GET.get("checkout", "")},
    )
