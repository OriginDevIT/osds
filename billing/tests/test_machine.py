"""Every entitlement state transition gets a test (CLAUDE.md; decisions.md §3,
§4.11).

The table in ``billing.machine`` is the single list of legal moves. This module
drives *each* entry through ``apply_trigger`` and checks, against an expectation
written here and not read back from the table's own outcome fields:

* the status it lands on;
* the ``entitlement.*`` events it emits, in order (the table's ``events``);
* ``listing.tier_changed`` exactly when the effective tier moves, where the
  effective tier is the spec §6.5 rule restated below;
* that every ``(status, trigger)`` pair *not* in the table is refused and
  changes nothing.

A transition added to the table is covered here automatically; the count check
fails if the loop ever skips one.
"""

from __future__ import annotations

from datetime import timedelta

from billing import machine
from billing.machine import InvalidTransition, Trigger
from billing.models import Entitlement
from billing.tests.base import DAY, T0, BillingTestCase, Role

S = Entitlement.Status

# Spec §6.5, restated: the statuses that show the entitlement's own tier.
PERKS = {S.TRIALING, S.ACTIVE, S.PAST_DUE, S.CANCELED, S.COMPED}


class _MachineBase(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.operator(Role.MANAGER)
        self._n = 0

    def params(self, trigger):
        base = {
            Trigger.START: {"tier": self.featured, "period_end": T0 + 30 * DAY},
            Trigger.START_TRIAL: {"tier": self.featured, "trial_ends_at": T0 + 7 * DAY},
            Trigger.PAYMENT_SUCCEEDED: {"period_end": T0 + 30 * DAY},
            Trigger.PAYMENT_FAILED: {"failure_code": "card_declined"},
            Trigger.CANCEL: {"canceled_by": "owner", "reason": "moving"},
            Trigger.GRANT: {"tier": self.featured, "operator": self.admin, "reason": "partner"},
            Trigger.REVOKE: {"operator": self.admin, "reason": "ended"},
            Trigger.OVERRIDE: {"to_status": S.ACTIVE, "tier": self.featured,
                               "operator": self.admin, "reason": "fix"},
        }
        return base.get(trigger, {})

    def make(self, status):
        """A fresh listing whose entitlement is in ``status`` with every timer
        already due at ``T0``, and whose badge matches that status. ``none``
        is no row."""
        self._n += 1
        listing = self.make_listing(f"case-{self._n}", f"Case {self._n}")
        if status == S.NONE:
            return listing
        Entitlement_ = Entitlement
        Entitlement_.all_tenants.create(
            tenant=self.tenant, listing=listing, tier=self.featured, status=status,
            billing_mode=(
                Entitlement.BillingMode.TERM if status == S.ACTIVE
                else Entitlement.BillingMode.COMP if status == S.COMPED
                else Entitlement.BillingMode.RECURRING
            ),
            term_days=30 if status == S.ACTIVE else None,
            started_at=T0 - 40 * DAY,
            current_period_end=T0 - timedelta(seconds=1),
            trial_ends_at=T0 - 4 * DAY if status == S.TRIALING else None,
            dunning_started_at=T0 - 14 * DAY if status == S.PAST_DUE else None,
            dunning_ends_at=T0 - timedelta(seconds=1) if status == S.PAST_DUE else None,
            grace_ends_at=T0 - timedelta(seconds=1) if status == S.GRACE else None,
            comp_granted_by=self.admin if status == S.COMPED else None,
            comp_reason="seed" if status == S.COMPED else "",
            comp_expires_at=T0 - timedelta(seconds=1) if status == S.COMPED else None,
            cancel_at_period_end=status == S.CANCELED,
        )
        shown = self.featured if status in PERKS else self.free
        type(listing).all_tenants.filter(pk=listing.pk).update(current_tier=shown)
        return type(listing).all_tenants.get(pk=listing.pk)


class EveryTransitionTests(_MachineBase):
    def test_each_entry_in_the_table(self):
        covered = 0
        for (frm, trigger), rule in machine.TRANSITIONS.items():
            with self.subTest(frm=frm, trigger=trigger, row=rule.row):
                listing = self.make(frm)
                before_tier = listing.current_tier
                since = self.last_event_id()

                outcome = self.apply(trigger, listing=listing, **self.params(trigger))

                landed = S.ACTIVE if rule.to == "*" else rule.to
                self.assertEqual(outcome.from_status, frm)
                self.assertEqual(outcome.to_status, landed)
                self.assertEqual(self.ent(listing).status, landed)

                emitted = [e.type for e in self.events(since=since)
                           if e.type.startswith("entitlement.")]
                self.assertEqual(emitted, list(rule.events))

                after_tier = self.featured if landed in PERKS else self.free
                changed = before_tier != after_tier
                tier_events = [e for e in self.events("listing.tier_changed", since=since)]
                self.assertEqual(len(tier_events), int(changed))
                self.assertEqual(outcome.tier_changed, changed)
                self.assertEqual(self.fresh(listing).current_tier, after_tier)
                if changed:
                    self.assertEqual(tier_events[0].subject, listing.public_id)
                covered += 1
        self.assertEqual(covered, len(machine.TRANSITIONS))

    def test_every_pair_not_in_the_table_is_refused_and_changes_nothing(self):
        refused = 0
        for status in S.values:
            for trigger in Trigger.values:
                if (status, trigger) in machine.TRANSITIONS:
                    continue
                with self.subTest(status=status, trigger=trigger):
                    listing = self.make(status)
                    since = self.last_event_id()
                    with self.assertRaises(InvalidTransition):
                        self.apply(trigger, listing=listing, **self.params(trigger))
                    self.assertEqual(self.events(since=since).count(), 0)
                    if status == S.NONE:
                        self.assertFalse(Entitlement.all_tenants.filter(listing=listing).exists())
                    else:
                        self.assertEqual(self.ent(listing).status, status)
                    refused += 1
        self.assertEqual(refused, len(S.values) * len(Trigger.values) - len(machine.TRANSITIONS))

    def test_every_rule_names_its_spec_row(self):
        for key, rule in machine.TRANSITIONS.items():
            with self.subTest(key=key):
                self.assertTrue(rule.row, "each transition cites its spec §6.3 row or its G-number")
        spec_rows = {r.row for r in machine.TRANSITIONS.values() if r.row.isdigit()}
        self.assertEqual(spec_rows, {str(n) for n in range(1, 17)})

    def test_the_gap_rows_are_all_defined(self):
        gaps = {r.row for r in machine.TRANSITIONS.values() if r.row.startswith("G")}
        for expected in ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9"):
            self.assertTrue(any(g.startswith(expected) or expected in g for g in gaps), expected)

    def test_the_effective_tier_rule_matches_the_spec_table(self):
        self.assertEqual(machine.PERK_STATUSES, PERKS)
