"""Template tags for the owner's billing panel and banner. They read the
entitlement themselves, so the owner views in ``directory`` need no billing
knowledge."""

from __future__ import annotations

from django import template
from django.utils import timezone

from billing import owner_state, payments
from billing.models import Entitlement, Tier

register = template.Library()


def _state(context, listing):
    request = context["request"]
    tenant = request.tenant
    ent = Entitlement.objects.select_related("tier").filter(listing=listing).first()
    tiers = list(
        Tier.objects.filter(purchasable=True, uses_slot=False, rank__gt=0).order_by("rank")
    )
    return owner_state.describe(
        ent, now=timezone.now(), payments_available=payments.available(tenant),
        sellable_tiers=tiers,
    )


@register.inclusion_tag("billing/owner/_banner.html", takes_context=True)
def owner_billing_banner(context, listing):
    return {"state": _state(context, listing)}


@register.inclusion_tag("billing/owner/_panel.html", takes_context=True)
def owner_billing_panel(context, listing):
    return {
        "state": _state(context, listing),
        "listing": listing,
        "csrf_token": context.get("csrf_token"),
    }
