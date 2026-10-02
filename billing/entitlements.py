"""The entitlement service: the one place a transition is applied and a
listing's tier is written (spec §6; decisions.md §4.11).

Core owns entitlement; adapters own money (CLAUDE.md invariant 2). A payment
adapter reports an outcome and core decides what it means: the adapter-driven
triggers (``start``, ``payment_succeeded``, ``payment_failed``, ``refund``,
``subscription_ended``) are applied here by the ``entitlement.reportPayment``
command in the next PR. This PR applies the clock's triggers (the worker tick)
and an operator's (comp, revoke, override).

``apply_trigger`` is the only function that moves an entitlement. It takes the
entitlement and then the listing ``FOR UPDATE`` -- in that order, always -- and
every transition is one transaction that writes the state change and every
event it causes.

``_set_tier`` is the only code that assigns ``Listing.current_tier`` (an AST
test fails if anything else does). It emits ``listing.tier_changed`` whenever
the *effective* tier moves: the tier whose perks the public page shows. That is
the entitlement's tier while it is in a perk status, else the tenant's rank-0
tier, else none.

Call with the tenant in ambient scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction

from audit import events
from audit.outbox import emit
from billing import machine, notices
from billing.machine import GRACE, DUNNING, Trigger
from billing.models import Entitlement, Tier
from directory import services as directory_services
from directory.models import Listing

S = Entitlement.Status

SYSTEM_ACTOR = {"type": "system", "id": "billing"}


class EntitlementError(ValueError):
    """A trigger's parameters are missing or wrong. Nothing is written."""


@dataclass
class Outcome:
    from_status: str
    to_status: str
    events: list = field(default_factory=list)
    tier_changed: bool = False
    hid_listing: bool = False
    unhid_listing: bool = False


# --- the effective tier and its one writer ----------------------------------


def rank0_tier(tenant) -> "Tier | None":
    return Tier.objects.filter(tenant=tenant, rank=0).first()


def effective_tier(tenant, ent: Entitlement) -> "Tier | None":
    """The tier whose perks the public page shows for ``ent`` (spec §6.5)."""
    if ent.status in machine.PERK_STATUSES:
        return ent.tier
    return rank0_tier(tenant)


def _set_tier(tenant, listing: Listing, tier: "Tier | None", *, cause: str, now, actor: dict,
              origin: str = "") -> bool:
    """The only write to ``Listing.current_tier`` in the codebase. Returns
    whether it changed. The listing row must already be locked."""
    new_id = tier.pk if tier is not None else None
    if listing.current_tier_id == new_id:
        return False
    before = listing.current_tier
    listing.current_tier = tier
    listing.save(update_fields=["current_tier", "updated_at"])
    emit(
        events.LISTING_TIER_CHANGED,
        subject=listing.public_id,
        tenant=tenant,
        actor=actor,
        origin=origin,
        data={
            "from_tier": before.key if before else None,
            "to_tier": tier.key if tier else None,
            "effective_at": now.isoformat(),
            "cause": cause,
        },
    )
    return True


# --- applying a trigger ------------------------------------------------------


def _need(params: dict, *names):
    missing = [n for n in names if params.get(n) is None]
    if missing:
        raise EntitlementError(f"missing parameter(s): {', '.join(missing)}")


def _reset_lifecycle(ent: Entitlement) -> None:
    ent.dunning_started_at = None
    ent.dunning_ends_at = None
    ent.grace_ends_at = None
    ent.failed_attempts = 0
    ent.last_failure_code = ""
    ent.cancel_at_period_end = False
    ent.canceled_at = None
    ent.canceled_by = ""
    ent.renewal_notified_at = None


def _clear_comp(ent: Entitlement) -> None:
    ent.comp_granted_by = None
    ent.comp_reason = ""
    ent.comp_expires_at = None


def _enter_dunning(ent: Entitlement, now, code: str, attempt) -> None:
    ent.dunning_started_at = now
    ent.dunning_ends_at = now + DUNNING
    ent.failed_attempts = int(attempt) if attempt else ent.failed_attempts + 1
    ent.last_failure_code = code or ""


def _mutate(ent: Entitlement, trigger: str, rule, now, p: dict) -> dict:
    """Apply ``trigger``'s field changes to ``ent`` and return the facts the
    events need. Writes nothing; ``apply_trigger`` saves."""
    T = Trigger
    facts: dict = {"from_tier": ent.tier.key if ent.tier_id else None}
    prior = ent.status

    if trigger in (T.START, T.START_TRIAL):
        _need(p, "tier")
        trial = trigger == T.START_TRIAL
        if trial:
            _need(p, "trial_ends_at")
        else:
            _need(p, "period_end")
        ent.tier = p["tier"]
        ent.billing_mode = p.get("billing_mode") or Entitlement.BillingMode.RECURRING
        ent.term_days = p.get("term_days")
        ent.status = S.TRIALING if trial else S.ACTIVE
        ent.started_at = now
        ent.trial_ends_at = p["trial_ends_at"] if trial else None
        ent.current_period_end = p.get("period_end") or p["trial_ends_at"]
        ent.payment_ref = p.get("payment_ref") or {}
        _reset_lifecycle(ent)
        _clear_comp(ent)

    elif trigger == T.PAYMENT_SUCCEEDED:
        _need(p, "period_end")
        if prior == S.PAST_DUE and ent.dunning_started_at:
            facts["days_in_dunning"] = (now - ent.dunning_started_at).days
        if prior == S.GRACE and ent.grace_ends_at:
            facts["days_in_grace"] = (now - (ent.grace_ends_at - GRACE)).days
        if prior == S.CANCELED:
            facts["days_in_grace"] = 0
        if p.get("tier") is not None:
            ent.tier = p["tier"]
        ent.status = S.ACTIVE
        ent.current_period_end = p["period_end"]
        if p.get("payment_ref"):
            ent.payment_ref = p["payment_ref"]
        if prior == S.COMPED:
            ent.billing_mode = p.get("billing_mode") or Entitlement.BillingMode.RECURRING
            _clear_comp(ent)
        _reset_lifecycle(ent)

    elif trigger in (T.PAYMENT_FAILED, T.TRIAL_OVERDUE):
        code = "trial_unreported" if trigger == T.TRIAL_OVERDUE else p.get("failure_code", "")
        if prior in (S.PAST_DUE, S.GRACE):
            ent.failed_attempts = int(p["attempt"]) if p.get("attempt") else ent.failed_attempts + 1
            ent.last_failure_code = code or ent.last_failure_code
        else:
            _enter_dunning(ent, now, code, p.get("attempt"))
            ent.status = S.PAST_DUE
            facts["attempt"] = ent.failed_attempts
            facts["dunning_ends_at"] = ent.dunning_ends_at.isoformat()
            facts["failure_code"] = ent.last_failure_code

    elif trigger == T.DUNNING_ELAPSED:
        ent.status = S.GRACE
        ent.grace_ends_at = now + GRACE
        facts["grace_ends_at"] = ent.grace_ends_at.isoformat()

    elif trigger == T.CANCEL:
        ent.canceled_at = now
        ent.canceled_by = p.get("canceled_by") or "owner"
        facts["canceled_by"] = ent.canceled_by
        facts["reason"] = p.get("reason", "")
        if rule.to == S.CANCELED:
            ent.status = S.CANCELED
            ent.cancel_at_period_end = True
            ent.current_period_end = ent.current_period_end or ent.trial_ends_at
            facts["at_period_end"] = True
        else:  # from grace: nothing left to run to
            ent.status = S.EXPIRED
            facts["at_period_end"] = False

    elif trigger in (
        T.REFUND, T.SUBSCRIPTION_ENDED, T.TERM_ENDED, T.GRACE_ELAPSED,
        T.PERIOD_ELAPSED, T.COMP_EXPIRED,
    ):
        ent.status = S.EXPIRED

    elif trigger == T.GRANT:
        _need(p, "tier", "operator", "reason")
        ent.tier = p["tier"]
        ent.status = S.COMPED
        ent.billing_mode = Entitlement.BillingMode.COMP
        ent.term_days = None
        ent.started_at = ent.started_at or now
        ent.current_period_end = None
        ent.trial_ends_at = None
        ent.comp_granted_by = p["operator"]
        ent.comp_reason = p["reason"]
        ent.comp_expires_at = p.get("expires_at")
        _reset_lifecycle(ent)

    elif trigger == T.REVOKE:
        _need(p, "operator", "reason")
        ent.status = S.EXPIRED
        _clear_comp(ent)

    elif trigger == T.OVERRIDE:
        _need(p, "to_status", "operator", "reason")
        to = p["to_status"]
        if to not in S.values:
            raise EntitlementError(f"unknown status {to!r}")
        if p.get("tier") is not None:
            ent.tier = p["tier"]
        ent.status = to
        if p.get("period_end") is not None:
            ent.current_period_end = p["period_end"]
        if to == S.TRIALING:
            _need(p, "trial_ends_at")
            ent.trial_ends_at = p["trial_ends_at"]
            ent.current_period_end = ent.current_period_end or p["trial_ends_at"]
        if to == S.PAST_DUE:
            _enter_dunning(ent, now, "override", None)
        if to == S.GRACE:
            ent.grace_ends_at = now + GRACE
        if to == S.CANCELED:
            _need(p, "period_end")
            ent.cancel_at_period_end = True
            ent.canceled_at = now
            ent.canceled_by = "operator"
        if to in (S.NONE, S.EXPIRED):
            _reset_lifecycle(ent)
        if to != S.COMPED:
            _clear_comp(ent)

    else:  # pragma: no cover - the table is closed over Trigger
        raise EntitlementError(f"unhandled trigger {trigger!r}")

    # What an override recorded "to" is what it landed on, not "*".
    facts["to_status"] = ent.status
    return facts


def _event_data(ent: Entitlement, name: str, rule, facts: dict, p: dict, now, prior: str) -> dict:
    base = {
        "entitlement_id": ent.public_id,
        "listing_id": ent.listing.public_id,
    }
    tier = ent.tier.key if ent.tier_id else None
    period_end = ent.current_period_end.isoformat() if ent.current_period_end else None
    if name == "entitlement.started":
        return {**base, "tier": tier, "billing_mode": ent.billing_mode,
                "period_end": period_end,
                "trial_ends_at": ent.trial_ends_at.isoformat() if ent.trial_ends_at else None}
    if name == "entitlement.trial_converted":
        return {**base, "tier": tier, "period_end": period_end}
    if name == "entitlement.dunning_started":
        return {**base, "attempt": facts["attempt"], "dunning_ends_at": facts["dunning_ends_at"],
                "failure_code": facts["failure_code"]}
    if name == "entitlement.recovered":
        return {**base, "days_in_dunning": facts.get("days_in_dunning", 0)}
    if name == "entitlement.downgraded":
        rank0 = facts.get("to_tier")
        return {**base, "from_tier": facts["from_tier"], "to_tier": rank0,
                "grace_ends_at": facts["grace_ends_at"]}
    if name == "entitlement.restored":
        return {**base, "tier": tier, "days_in_grace": facts.get("days_in_grace", 0)}
    if name == "entitlement.expired":
        return {**base, "from_tier": facts["from_tier"], "cause": rule.expired_cause}
    if name == "entitlement.canceled":
        return {**base, "at_period_end": facts["at_period_end"],
                "reason": facts.get("reason", ""), "canceled_by": facts["canceled_by"]}
    if name == "entitlement.overridden":
        return {**base, "admin_id": p["operator"].public_id, "reason": p["reason"],
                "from": prior, "to": ent.status}
    raise EntitlementError(f"unknown event {name!r}")  # pragma: no cover


def _tier_cause(rule, prior_tier: "Tier | None", target: "Tier | None") -> str:
    """``listing.tier_changed``'s cause. A payment that moves the tier is an
    upgrade or a downgrade by rank; every other cause is the rule's."""
    if rule.tier_cause in ("", "upgrade"):
        if target is not None and (prior_tier is None or target.rank >= prior_tier.rank):
            return "upgrade"
        return "downgrade"
    return rule.tier_cause


def apply_trigger(
    tenant, listing: Listing, trigger: str, *, now, actor: dict = SYSTEM_ACTOR,
    origin: str = "", **params
) -> Outcome:
    """Apply ``trigger`` to ``listing``'s entitlement, creating the row on a
    first start or grant. Raises ``InvalidTransition`` for a move the table does
    not allow, ``NotDue`` for a clock trigger whose condition no longer holds,
    ``EntitlementError`` for bad parameters; nothing is written in any case.
    """
    with transaction.atomic():
        ent = (
            Entitlement.objects.select_for_update(of=("self",))
            .select_related("tier")
            .filter(listing=listing)
            .first()
        )
        listing = (
            Listing.objects.select_for_update(of=("self",))
            .select_related("current_tier")
            .get(pk=listing.pk)
        )

        if ent is None:
            if trigger not in (Trigger.START, Trigger.START_TRIAL, Trigger.GRANT, Trigger.OVERRIDE):
                raise machine.InvalidTransition(S.NONE, trigger)
            tier = params.get("tier")
            if tier is None:
                raise EntitlementError("missing parameter(s): tier")
            ent = Entitlement.objects.create(
                tenant=tenant, listing=listing, tier=tier, status=S.NONE,
                billing_mode=Entitlement.BillingMode.NONE,
            )

        prior = ent.status
        rule = machine.rule_for(prior, trigger)
        if trigger in machine.CLOCK_TRIGGERS and not machine.is_due(ent, trigger, now):
            raise machine.NotDue(trigger)

        prior_tier = listing.current_tier
        facts = _mutate(ent, trigger, rule, now, params)
        ent.save()

        # The effective tier, and the rank-0 name the downgrade event carries.
        target = effective_tier(tenant, ent)
        facts["to_tier"] = target.key if target else None

        out = Outcome(from_status=prior, to_status=ent.status)
        for name in rule.events:
            emit(
                name,
                subject=ent.public_id,
                tenant=tenant,
                actor=actor,
                origin=origin,
                data=_event_data(ent, name, rule, facts, params, now, prior),
            )
            out.events.append(name)

        out.tier_changed = _set_tier(
            tenant, listing, target, cause=_tier_cause(rule, prior_tier, target),
            now=now, actor=actor, origin=origin,
        )
        _visibility_after(tenant, ent, listing, out, actor=actor)
        notices.after_transition(tenant, ent, listing, rule, now=now)
        return out


def _visibility_after(tenant, ent: Entitlement, listing: Listing, out: Outcome, *, actor) -> None:
    """Spec §6.4: with no rank-0 tier an expired listing is hidden; a repurchase
    undoes only that, and only if nothing else has changed it since."""
    if ent.status == S.EXPIRED and rank0_tier(tenant) is None:
        if listing.visibility == Listing.Visibility.PUBLISHED:
            directory_services.set_listing_visibility(
                listing, Listing.Visibility.HIDDEN, actor=actor, reason="entitlement_expired"
            )
            ent.hidden_by_expiry = True
            ent.save(update_fields=["hidden_by_expiry", "updated_at"])
            out.hid_listing = True
    elif ent.hidden_by_expiry and ent.status in machine.PERK_STATUSES:
        if listing.visibility == Listing.Visibility.HIDDEN:
            directory_services.set_listing_visibility(
                listing, Listing.Visibility.PUBLISHED, actor=actor, reason="entitlement_restored"
            )
            out.unhid_listing = True
        ent.hidden_by_expiry = False
        ent.save(update_fields=["hidden_by_expiry", "updated_at"])
