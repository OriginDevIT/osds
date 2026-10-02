"""The entitlement state machine, as data (spec §6.2, §6.3; decisions.md §4.11).

Nothing here touches the database. ``TRANSITIONS`` is the whole table: a key
``(from_status, trigger)`` is a legal move, anything else is refused. The test
module iterates this table and fails if an entry has no expected outcome, so a
transition added here without a test cannot merge ("every entitlement state
transition needs a test", CLAUDE.md).

``from_status`` ``none`` is the absence of an entitlement row for a listing.

Rows 1-16 are spec §6.3. The ``G`` rows are transitions the spec leaves
undefined and decisions.md §4.11 defines:

* G1 an expired (or never-purchased) listing buys again;
* G2 a canceled owner resubscribes before the period ends;
* G3 cancelling from past_due skips grace; from grace it ends at once;
* G4 a refund in any paid state expires it (spec: only ``active``);
* G5 a comped listing that buys becomes active;
* G6 an active renewal, or a tier change, stays active;
* G7 the adapter reports the subscription ended outright;
* G8 a trial that the payment adapter never reports on goes to past_due;
* G9 ``entitlement.revoke`` ends a comp.

``listing.tier_changed`` is never listed per row: the service emits it whenever
the *effective* tier changes, so a row cannot forget it and cannot invent it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db import models

from billing.models import Entitlement

S = Entitlement.Status

DUNNING = timedelta(days=14)  # past_due -> grace (spec §6.3)
GRACE = timedelta(days=30)  # grace -> expired (spec §6.3)
TRIAL_OVERDUE = timedelta(days=3)  # G8: how long a trial may go unreported
RENEWAL_NOTICE = timedelta(days=10)  # spec §6.6 T-10, term entitlements


class Trigger(models.TextChoices):
    # Reported by a payment adapter (PR 2) -- never by a person.
    START = "start", "Checkout completed"
    START_TRIAL = "start_trial", "Checkout completed, trial"
    PAYMENT_SUCCEEDED = "payment_succeeded", "Payment succeeded"
    PAYMENT_FAILED = "payment_failed", "Payment failed"
    REFUND = "refund", "Refund issued"
    SUBSCRIPTION_ENDED = "subscription_ended", "Subscription ended"
    # A person's decision.
    CANCEL = "cancel", "Cancelled"
    # The clock, on the worker tick.
    TERM_ENDED = "term_ended", "Term ended"
    DUNNING_ELAPSED = "dunning_elapsed", "Dunning window elapsed"
    GRACE_ELAPSED = "grace_elapsed", "Grace window elapsed"
    PERIOD_ELAPSED = "period_elapsed", "Cancelled period ended"
    COMP_EXPIRED = "comp_expired", "Comp expired"
    TRIAL_OVERDUE = "trial_overdue", "Trial unreported"
    # An operator.
    GRANT = "grant", "Comp granted"
    REVOKE = "revoke", "Comp revoked"
    OVERRIDE = "override", "Overridden"


# Statuses whose perks the public page shows in full (spec §6.5). Everything
# else sits on the rank-0 tier.
PERK_STATUSES = frozenset(
    {S.TRIALING, S.ACTIVE, S.PAST_DUE, S.CANCELED, S.COMPED}
)

# Statuses that are paid-for or paid-and-lapsing: where a refund, an ended
# subscription and a cancel apply.
PAID_STATUSES = (S.TRIALING, S.ACTIVE, S.PAST_DUE, S.GRACE, S.CANCELED)

ANY_STATUS = tuple(S.values)


@dataclass(frozen=True)
class Rule:
    """The outcome of one legal ``(from, trigger)``: where it lands and which
    ``entitlement.*`` events it emits. ``expired_cause`` is set when the target
    is ``expired``. ``tier_cause`` is the ``listing.tier_changed`` cause if the
    effective tier moves."""

    to: str
    events: tuple = ()
    expired_cause: str = ""
    tier_cause: str = ""
    row: str = ""  # the spec §6.3 row number, or the G-number


def _rule(to, events=(), *, cause="", tier="", row=""):
    return Rule(to=to, events=tuple(events), expired_cause=cause, tier_cause=tier, row=row)


STARTED = "entitlement.started"
CONVERTED = "entitlement.trial_converted"
DUNNING_STARTED = "entitlement.dunning_started"
RECOVERED = "entitlement.recovered"
DOWNGRADED = "entitlement.downgraded"
RESTORED = "entitlement.restored"
EXPIRED = "entitlement.expired"
CANCELED = "entitlement.canceled"
OVERRIDDEN = "entitlement.overridden"


def _build() -> dict:
    t: dict = {}

    def add(frm, trigger, rule):
        key = (frm, trigger)
        assert key not in t, key
        t[key] = rule

    T = Trigger
    # -- starting (rows 1, 2; G1, G2, G5) ---------------------------------------
    for frm in (S.NONE, S.EXPIRED, S.COMPED, S.CANCELED):
        add(frm, T.START, _rule(S.ACTIVE, [STARTED], tier="purchase", row="2" if frm == S.NONE else "G1/G2/G5"))
    for frm in (S.NONE, S.EXPIRED):
        add(frm, T.START_TRIAL, _rule(S.TRIALING, [STARTED], tier="purchase", row="1" if frm == S.NONE else "G1"))

    # -- trialing (rows 3, 4, 5; G4, G7, G8) -------------------------------------
    add(S.TRIALING, T.PAYMENT_SUCCEEDED, _rule(S.ACTIVE, [CONVERTED], row="3"))
    add(S.TRIALING, T.PAYMENT_FAILED, _rule(S.PAST_DUE, [DUNNING_STARTED], row="4"))
    add(S.TRIALING, T.CANCEL, _rule(S.CANCELED, [CANCELED], row="5"))
    add(S.TRIALING, T.TRIAL_OVERDUE, _rule(S.PAST_DUE, [DUNNING_STARTED], row="G8"))

    # -- active (rows 6, 7, 8, 9; G6) --------------------------------------------
    add(S.ACTIVE, T.PAYMENT_SUCCEEDED, _rule(S.ACTIVE, [], tier="upgrade", row="G6"))
    add(S.ACTIVE, T.PAYMENT_FAILED, _rule(S.PAST_DUE, [DUNNING_STARTED], row="6"))
    add(S.ACTIVE, T.CANCEL, _rule(S.CANCELED, [CANCELED], row="7"))
    add(S.ACTIVE, T.TERM_ENDED, _rule(S.EXPIRED, [EXPIRED], cause="term_ended", tier="expiry", row="9"))

    # -- past_due (rows 10, 11; G3) ----------------------------------------------
    add(S.PAST_DUE, T.PAYMENT_SUCCEEDED, _rule(S.ACTIVE, [RECOVERED], row="10"))
    add(S.PAST_DUE, T.PAYMENT_FAILED, _rule(S.PAST_DUE, [], row="G6"))
    add(S.PAST_DUE, T.DUNNING_ELAPSED, _rule(S.GRACE, [DOWNGRADED], tier="grace", row="11"))
    add(S.PAST_DUE, T.CANCEL, _rule(S.CANCELED, [CANCELED], row="G3"))

    # -- grace (rows 12, 13; G3) -------------------------------------------------
    add(S.GRACE, T.PAYMENT_SUCCEEDED, _rule(S.ACTIVE, [RESTORED], tier="restore", row="12"))
    add(S.GRACE, T.PAYMENT_FAILED, _rule(S.GRACE, [], row="G6"))
    add(S.GRACE, T.GRACE_ELAPSED, _rule(S.EXPIRED, [EXPIRED], cause="grace_elapsed", tier="expiry", row="13"))
    add(S.GRACE, T.CANCEL, _rule(S.EXPIRED, [CANCELED, EXPIRED], cause="canceled", tier="expiry", row="G3"))

    # -- canceled (row 14; G2) ---------------------------------------------------
    add(S.CANCELED, T.PAYMENT_SUCCEEDED, _rule(S.ACTIVE, [RESTORED], row="G2"))
    add(S.CANCELED, T.PERIOD_ELAPSED, _rule(S.EXPIRED, [EXPIRED], cause="period_ended", tier="expiry", row="14"))

    # -- comped (row 15; G5, G9) -------------------------------------------------
    add(S.COMPED, T.PAYMENT_SUCCEEDED, _rule(S.ACTIVE, [], tier="purchase", row="G5"))
    add(S.COMPED, T.COMP_EXPIRED, _rule(S.EXPIRED, [EXPIRED], cause="comp_expired", tier="expiry", row="15"))
    add(S.COMPED, T.REVOKE, _rule(S.EXPIRED, [OVERRIDDEN, EXPIRED], cause="revoked", tier="revoke", row="G9"))

    # -- refund and an ended subscription, in every paid state (row 8; G4, G7) ---
    for frm in PAID_STATUSES:
        add(frm, T.REFUND, _rule(S.EXPIRED, [EXPIRED], cause="refund", tier="refund", row="8" if frm == S.ACTIVE else "G4"))
        add(frm, T.SUBSCRIPTION_ENDED, _rule(S.EXPIRED, [EXPIRED], cause="canceled", tier="expiry", row="G7"))

    # -- an operator's comp and override (row 16) --------------------------------
    for frm in ANY_STATUS:
        add(frm, T.GRANT, _rule(S.COMPED, [OVERRIDDEN], tier="comp", row="16"))
        add(frm, T.OVERRIDE, _rule("*", [OVERRIDDEN], tier="override", row="16"))
    return t


TRANSITIONS: "dict[tuple[str, str], Rule]" = _build()


class InvalidTransition(Exception):
    def __init__(self, status: str, trigger: str):
        self.status, self.trigger = status, trigger
        super().__init__(f"{trigger!r} is not legal from {status!r}")


class NotDue(Exception):
    """A clock trigger whose condition does not hold (yet, or any more): the
    row moved under the tick. Not an error; the job skips it."""


def rule_for(status: str, trigger: str) -> Rule:
    try:
        return TRANSITIONS[(status, trigger)]
    except KeyError:
        raise InvalidTransition(status, trigger) from None


def is_due(ent, trigger: str, now) -> bool:
    """Whether the clock trigger's condition holds for ``ent`` at ``now``."""
    if trigger == Trigger.DUNNING_ELAPSED:
        return ent.status == S.PAST_DUE and ent.dunning_ends_at is not None and ent.dunning_ends_at <= now
    if trigger == Trigger.GRACE_ELAPSED:
        return ent.status == S.GRACE and ent.grace_ends_at is not None and ent.grace_ends_at <= now
    if trigger == Trigger.PERIOD_ELAPSED:
        return ent.status == S.CANCELED and ent.current_period_end is not None and ent.current_period_end <= now
    if trigger == Trigger.COMP_EXPIRED:
        return ent.status == S.COMPED and ent.comp_expires_at is not None and ent.comp_expires_at <= now
    if trigger == Trigger.TERM_ENDED:
        return (
            ent.status == S.ACTIVE
            and ent.billing_mode == Entitlement.BillingMode.TERM
            and ent.current_period_end is not None
            and ent.current_period_end <= now
        )
    if trigger == Trigger.TRIAL_OVERDUE:
        return (
            ent.status == S.TRIALING
            and ent.trial_ends_at is not None
            and ent.trial_ends_at + TRIAL_OVERDUE <= now
        )
    return True


CLOCK_TRIGGERS = (
    Trigger.DUNNING_ELAPSED,
    Trigger.GRACE_ELAPSED,
    Trigger.PERIOD_ELAPSED,
    Trigger.COMP_EXPIRED,
    Trigger.TERM_ENDED,
    Trigger.TRIAL_OVERDUE,
)
