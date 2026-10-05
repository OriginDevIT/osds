"""Tier configuration (spec §4.2, decisions.md §4.11).

A tier is configuration, like a listing type: writing one emits
``tenant.settings_changed`` with a ``/tiers/{key}`` pointer, and the admin UI is
the only caller. Tiers are tenant-scoped rows; the rank-0 tier is the fallback
and the free one.

Slots are post-launch, so a tier with ``uses_slot`` is refused until they land.
"""

from __future__ import annotations

import re

from django.db import IntegrityError, transaction

from audit import events
from audit.command_log import settings_command
from audit.outbox import emit
from billing.models import Entitlement, Tier
from directory.models import Listing

_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class TierError(ValueError):
    """A tier value core refuses. Nothing is written."""


def tier_value(tier: Tier) -> dict:
    return {
        "key": tier.key,
        "name": tier.name,
        "rank": tier.rank,
        "purchasable": tier.purchasable,
        "uses_slot": tier.uses_slot,
        "price_minor": tier.price_minor,
        "currency": tier.currency,
        "interval": tier.interval,
        "trial_days": tier.trial_days,
        "badge_label": tier.badge_label,
        "perks": tier.perks,
    }


def _validate(*, key, name, rank, purchasable, uses_slot, price_minor, currency, badge_label="",
              interval, trial_days, perks) -> None:
    errors = []
    if not _KEY.match(key or ""):
        errors.append("The key is lowercase letters, digits and hyphens.")
    if not (name or "").strip():
        errors.append("A name is required.")
    if rank is None or rank < 0:
        errors.append("The rank is zero or more.")
    if uses_slot:
        errors.append("Slot-backed tiers are not available yet.")
    if not isinstance(perks, dict):
        errors.append("Perks must be an object.")
    if purchasable:
        if rank == 0:
            errors.append("The rank-0 tier is the free fallback and cannot be purchasable.")
        if not price_minor or price_minor <= 0:
            errors.append("A purchasable tier needs a price above zero.")
        if not _CURRENCY.match(currency or ""):
            errors.append("The currency is a three-letter ISO 4217 code, such as USD.")
        if interval not in Tier.Interval.values:
            errors.append("Choose how often it bills.")
        if trial_days is not None and not (1 <= trial_days <= 90):
            errors.append("A trial is 1 to 90 days.")
    else:
        if price_minor or currency or interval or trial_days:
            errors.append("Only a purchasable tier has a price, interval or trial.")
    if errors:
        raise TierError(" ".join(errors))


def _emit(tenant, *, actor, op: str, tier: Tier) -> None:
    emit(
        events.TENANT_SETTINGS_CHANGED,
        subject=tenant.public_id,
        tenant=tenant,
        actor={"type": "admin", "id": actor.public_id},
        data={
            "changes": [{"op": op, "path": f"/tiers/{tier.key}", "value": tier_value(tier)}],
            "changed_by": actor.public_id,
        },
    )


def create_tier(tenant, *, key, name, rank, purchasable=False, uses_slot=False,
                price_minor=None, currency="", interval="", trial_days=None,
                badge_label="", perks=None, actor) -> Tier:
    """An operator command: each tier write is one ``settings.update`` row naming
    the fields it set, never their values (decisions.md §4), so it runs in
    autocommit."""
    with settings_command(tenant, actor, page="tiers", op="add", refused=(TierError,)) as cmd:
        tier = _create_tier(
            tenant, key=key, name=name, rank=rank, purchasable=purchasable, uses_slot=uses_slot,
            price_minor=price_minor, currency=currency, interval=interval,
            trial_days=trial_days, badge_label=badge_label, perks=perks, actor=actor,
        )
        cmd.named(*tier_value(tier))
    return tier


@transaction.atomic
def _create_tier(tenant, *, key, name, rank, purchasable, uses_slot, price_minor, currency,
                 interval, trial_days, badge_label, perks, actor) -> Tier:
    perks = {} if perks is None else perks
    currency = (currency or "").upper()
    _validate(key=key, name=name, rank=rank, purchasable=purchasable, uses_slot=uses_slot,
              price_minor=price_minor, currency=currency, interval=interval,
              trial_days=trial_days, perks=perks)
    try:
        with transaction.atomic():
            tier = Tier.objects.create(
                tenant=tenant, key=key, name=name.strip(), rank=rank,
                purchasable=purchasable, uses_slot=False, price_minor=price_minor,
                currency=currency, interval=interval, trial_days=trial_days,
                badge_label=badge_label, perks=perks,
            )
    except IntegrityError as exc:
        raise TierError("A tier with that key or rank already exists.") from exc
    _emit(tenant, actor=actor, op="add", tier=tier)
    return tier


def update_tier(tier: Tier, *, actor, **changes) -> Tier:
    """Change a tier. ``key`` is the tier's identity and cannot change. Logged like
    ``create_tier``; a save that changes nothing writes a row naming nothing."""
    with settings_command(tier.tenant, actor, page="tiers", refused=(TierError,)) as cmd:
        before = tier_value(tier)
        _update_tier(tier, actor=actor, **changes)
        cmd.changed(before, tier_value(tier))
    return tier


@transaction.atomic
def _update_tier(tier: Tier, *, actor, **changes) -> Tier:
    if "key" in changes and changes["key"] != tier.key:
        raise TierError("A tier's key cannot be changed.")
    changes.pop("key", None)
    fields = {
        "name": tier.name, "rank": tier.rank, "purchasable": tier.purchasable,
        "uses_slot": tier.uses_slot, "price_minor": tier.price_minor,
        "currency": tier.currency, "interval": tier.interval,
        "trial_days": tier.trial_days, "badge_label": tier.badge_label,
        "perks": tier.perks,
    }
    unknown = set(changes) - set(fields)
    if unknown:
        raise TierError(f"Unknown field(s): {', '.join(sorted(unknown))}")
    fields.update(changes)
    fields["currency"] = (fields["currency"] or "").upper()
    _validate(key=tier.key, **fields)
    before = tier_value(tier)
    try:
        with transaction.atomic():
            for name, value in fields.items():
                setattr(tier, name, value.strip() if name == "name" else value)
            tier.uses_slot = False
            tier.save()
    except IntegrityError as exc:
        raise TierError("A tier with that rank already exists.") from exc
    if tier_value(tier) != before:
        _emit(tenant=tier.tenant, actor=actor, op="replace", tier=tier)
    return tier


def delete_tier(tier: Tier, *, actor) -> None:
    """Refused while anything points at the tier: an entitlement, or a listing
    currently showing it (deleting would silently blank its badge). Logged like
    ``create_tier``, naming no fields."""
    with settings_command(tier.tenant, actor, page="tiers", op="remove", refused=(TierError,)):
        _delete_tier(tier, actor=actor)


@transaction.atomic
def _delete_tier(tier: Tier, *, actor) -> None:
    if Entitlement.objects.filter(tier=tier).exists():
        raise TierError("Listings hold entitlements on this tier; it cannot be deleted.")
    if Listing.objects.filter(current_tier=tier).exists():
        raise TierError("Listings currently show this tier; it cannot be deleted.")
    tenant, snapshot = tier.tenant, tier_value(tier)
    key = tier.key
    tier.delete()
    emit(
        events.TENANT_SETTINGS_CHANGED,
        subject=tenant.public_id,
        tenant=tenant,
        actor={"type": "admin", "id": actor.public_id},
        data={
            "changes": [{"op": "remove", "path": f"/tiers/{key}", "value": snapshot}],
            "changed_by": actor.public_id,
        },
    )
