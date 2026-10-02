"""What an owner sees about their listing's plan (spec §6.5; decisions.md
§4.11). Pure functions over an entitlement: the banner, and which actions to
offer.

The comp row is the point: a comped listing shows *nothing* about billing, so
the owner of a comp never learns it is one and has no plan to cancel or buy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from billing.models import Entitlement

S = Entitlement.Status


@dataclass
class OwnerBilling:
    kind: str = ""  # "", "info", "warn", "upsell"
    text: str = ""
    plan_line: str = ""
    can_buy: bool = False
    can_cancel: bool = False
    portal_label: str = ""  # "Update card" | "Restore" | ""
    tiers: list = field(default_factory=list)


def describe(ent, *, now, payments_available: bool, sellable_tiers) -> OwnerBilling:
    status = ent.status if ent else S.NONE
    out = OwnerBilling()
    has_ref = bool(ent and ent.payment_ref)

    if status == S.COMPED:
        return out  # nothing indicating comp status

    if status in (S.NONE, S.EXPIRED):
        out.kind = "upsell"
        out.text = (
            "Your upgraded placement has ended. Upgrade to get it back."
            if status == S.EXPIRED else "Upgrade your listing."
        )
        out.can_buy = payments_available and bool(sellable_tiers)
        if not out.can_buy:
            return OwnerBilling()  # nothing to offer, so nothing to show
        out.tiers = list(sellable_tiers)
        return out

    plan = f"{ent.tier.name}"
    if status == S.TRIALING:
        left = max(0, (ent.trial_ends_at - now).days) if ent.trial_ends_at else 0
        out.kind, out.text = "info", f"Trial ends in {left} day{'s' if left != 1 else ''}."
        out.can_cancel = True
    elif status == S.ACTIVE:
        out.plan_line = f"{plan}" + (
            f", renews {ent.current_period_end:%Y-%m-%d}" if ent.current_period_end else ""
        )
        out.can_cancel = True
    elif status == S.PAST_DUE:
        out.kind, out.text = "warn", "Payment failed, update card."
        out.portal_label = "Update card"
        out.can_cancel = True
    elif status == S.GRACE:
        out.kind, out.text = "warn", "Your listing has been downgraded. Restore it."
        out.portal_label = "Restore"
        out.can_cancel = True
    elif status == S.CANCELED:
        end = f"{ent.current_period_end:%Y-%m-%d}" if ent.current_period_end else "the end of the period"
        out.kind, out.text = "info", f"Cancelled, active until {end}."
        # Resubscribing before the period ends is a new checkout (G2).
        out.can_buy = payments_available and bool(sellable_tiers)
        out.tiers = list(sellable_tiers) if out.can_buy else []

    if out.portal_label and not (has_ref and payments_available):
        out.portal_label = ""
    return out
