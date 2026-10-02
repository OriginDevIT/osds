"""The operator's tier and entitlement pages (decisions.md §4.11).

Tiers and their pricing are the admin's (spec §4.4: "tiers and pricing");
comps, revokes and overrides are the manager's. Each POST route calls exactly
one command, which checks the role again.
"""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from audit.models import OutboxEvent
from billing import commands, tiers
from billing.forms import GrantCompForm, OverrideForm, RevokeForm, TierForm
from billing.models import Entitlement, Tier
from directory.access import tenant_admin_required
from directory.models import Listing
from tenants.models import StaffMembership

_ADMIN = tenant_admin_required(StaffMembership.Role.ADMIN)
_MANAGER = tenant_admin_required(commands.MANAGE_ROLE)


# --- tiers -------------------------------------------------------------------


@_ADMIN
def tier_list(request):
    rows = list(Tier.objects.order_by("rank"))
    return render(
        request,
        "billing/admin/tier_list.html",
        {"tiers": rows, "no_rank0": not any(t.rank == 0 for t in rows)},
    )


def _tier_initial(tier: Tier) -> dict:
    import json

    return {
        "key": tier.key, "name": tier.name, "rank": tier.rank,
        "purchasable": tier.purchasable, "price_minor": tier.price_minor,
        "currency": tier.currency, "interval": tier.interval,
        "trial_days": tier.trial_days, "badge_label": tier.badge_label,
        "perks": json.dumps(tier.perks) if tier.perks else "",
    }


@_ADMIN
def tier_create(request):
    form = TierForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            tier = tiers.create_tier(
                request.tenant, key=form.cleaned_data["key"], actor=request.user,
                **form.tier_kwargs(),
            )
        except tiers.TierError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Created tier “{tier.key}”.")
            return redirect("billing_admin:tier-list")
    return render(request, "billing/admin/tier_form.html", {"form": form, "mode": "create"})


@_ADMIN
def tier_edit(request, key):
    tier = get_object_or_404(Tier.objects, key=key)
    form = TierForm(request.POST or None, initial=_tier_initial(tier), editing=True)
    if request.method == "POST" and form.is_valid():
        try:
            tiers.update_tier(tier, actor=request.user, **form.tier_kwargs())
        except tiers.TierError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Saved.")
            return redirect("billing_admin:tier-list")
    return render(
        request, "billing/admin/tier_form.html", {"form": form, "mode": "edit", "tier": tier}
    )


@_ADMIN
@require_POST
def tier_delete(request, key):
    tier = get_object_or_404(Tier.objects, key=key)
    try:
        tiers.delete_tier(tier, actor=request.user)
    except tiers.TierError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Deleted tier “{key}”.")
    return redirect("billing_admin:tier-list")


# --- entitlements ------------------------------------------------------------


@_MANAGER
def entitlement_list(request):
    status = request.GET.get("status", "")
    rows = Entitlement.objects.select_related("listing", "tier").order_by("-updated_at")
    if status in Entitlement.Status.values:
        rows = rows.filter(status=status)
    tier_rows = list(Tier.objects.filter(uses_slot=False).order_by("rank"))
    return render(
        request,
        "billing/admin/entitlement_list.html",
        {
            "rows": rows[:200],
            "status": status,
            "statuses": Entitlement.Status.choices,
            "grant_form": GrantCompForm(tiers=tier_rows),
        },
    )


def _detail_context(request, ent, *, override_form=None, revoke_form=None):
    tier_rows = list(Tier.objects.filter(uses_slot=False).order_by("rank"))
    timeline = OutboxEvent.all_tenants.filter(
        tenant=request.tenant, subject__in=[ent.public_id, ent.listing.public_id],
        type__startswith="entitlement.",
    ) | OutboxEvent.all_tenants.filter(
        tenant=request.tenant, subject=ent.listing.public_id, type="listing.tier_changed"
    )
    return {
        "ent": ent,
        "timeline": timeline.order_by("id"),
        "override_form": override_form or OverrideForm(tiers=tier_rows),
        "revoke_form": revoke_form or RevokeForm(),
    }


@_MANAGER
def entitlement_detail(request, public_id):
    ent = get_object_or_404(
        Entitlement.objects.select_related("listing", "tier", "comp_granted_by"),
        public_id=public_id,
    )
    return render(request, "billing/admin/entitlement_detail.html", _detail_context(request, ent))


def _refused(request, exc):
    messages.error(request, commands.refusal_text(exc.reason))


@_MANAGER
@require_POST
def entitlement_grant(request):
    tier_rows = list(Tier.objects.filter(uses_slot=False).order_by("rank"))
    form = GrantCompForm(request.POST, tiers=tier_rows)
    if not form.is_valid():
        messages.error(request, "Fill in the listing, tier and reason.")
        return redirect("billing_admin:entitlement-list")
    listing = Listing.objects.filter(public_id=form.cleaned_data["listing"].strip()).first()
    if listing is None:
        messages.error(request, commands.refusal_text("listing_unknown"))
        return redirect("billing_admin:entitlement-list")
    try:
        commands.grant_comp(
            request.tenant, listing=listing, operator=request.user,
            tier_key=form.cleaned_data["tier"], reason=form.cleaned_data["reason"],
            expires_at=form.cleaned_data["expires_at"],
        )
    except commands.EntitlementRefused as exc:
        _refused(request, exc)
        return redirect("billing_admin:entitlement-list")
    ent = Entitlement.objects.get(listing=listing)
    messages.success(request, "Comp granted.")
    return redirect("billing_admin:entitlement-detail", public_id=ent.public_id)


@_MANAGER
@require_POST
def entitlement_revoke(request, public_id):
    ent = get_object_or_404(Entitlement.objects.select_related("listing"), public_id=public_id)
    form = RevokeForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    try:
        commands.revoke_comp(
            request.tenant, listing=ent.listing, operator=request.user, reason=reason
        )
    except commands.EntitlementRefused as exc:
        _refused(request, exc)
    else:
        messages.success(request, "Comp revoked.")
    return redirect("billing_admin:entitlement-detail", public_id=public_id)


@_MANAGER
@require_POST
def entitlement_override(request, public_id):
    ent = get_object_or_404(Entitlement.objects.select_related("listing"), public_id=public_id)
    tier_rows = list(Tier.objects.filter(uses_slot=False).order_by("rank"))
    form = OverrideForm(request.POST, tiers=tier_rows)
    if not form.is_valid():
        messages.error(request, "Choose a status and give a reason.")
        return redirect("billing_admin:entitlement-detail", public_id=public_id)
    cd = form.cleaned_data
    try:
        commands.override_entitlement(
            request.tenant, listing=ent.listing, operator=request.user,
            to_status=cd["to_status"], reason=cd["reason"], tier_key=cd["tier"],
            period_end=cd["period_end"], trial_ends_at=cd["trial_ends_at"],
        )
    except commands.EntitlementRefused as exc:
        _refused(request, exc)
    else:
        messages.success(request, "Entitlement overridden.")
    return redirect("billing_admin:entitlement-detail", public_id=public_id)
