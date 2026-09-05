"""The event-name registry is the single source of truth for event types.

These tests keep the named constants and ``ALL_EVENT_TYPES`` in lockstep, and
pin the catalogue to the spec so a change to either is deliberate.
"""

from __future__ import annotations

import re

from django.test import SimpleTestCase

from audit import events

# <namespace>.<past_tense_verb>, both lower_snake.
_NAME_RE = re.compile(r"^[a-z]+(?:_[a-z]+)*\.[a-z]+(?:_[a-z]+)*$")

# spec §3.3, minus the deferred namespaces in §3.4.
_EXPECTED_NAMESPACES = {
    "listing",
    "claim",
    "user",
    "staff",
    "billing",
    "entitlement",
    "slot",
    "lead",
    "call",
    "review",
    "moderation",
    "compliance",
    "agent",
    "tenant",
    "import",
    "postal",
}

# spec §3.3 catalogue. Update this alongside the spec, never to chase a typo.
_EXPECTED_COUNT = 77


def _named_constants() -> dict[str, str]:
    """Every module-level ``UPPER_CASE`` attribute whose value is a dotted string."""
    out = {}
    for attr in dir(events):
        if not attr.isupper():
            continue
        value = getattr(events, attr)
        if isinstance(value, str) and "." in value:
            out[attr] = value
    return out


class EventRegistryTests(SimpleTestCase):
    def test_all_event_types_is_a_frozenset(self):
        self.assertIsInstance(events.ALL_EVENT_TYPES, frozenset)

    def test_named_constants_and_registry_agree(self):
        constants = _named_constants()
        self.assertEqual(
            set(constants.values()),
            set(events.ALL_EVENT_TYPES),
            "every named constant must be in ALL_EVENT_TYPES and vice versa",
        )

    def test_no_duplicate_constant_values(self):
        values = list(_named_constants().values())
        self.assertEqual(sorted(values), sorted(set(values)))

    def test_names_are_well_formed(self):
        bad = [t for t in events.ALL_EVENT_TYPES if not _NAME_RE.match(t)]
        self.assertEqual(bad, [], "event names must be <namespace>.<past_tense_verb>")

    def test_namespaces_match_the_spec(self):
        self.assertEqual(set(events.NAMESPACES), _EXPECTED_NAMESPACES)

    def test_deferred_namespaces_are_absent(self):
        present = {
            t for t in events.ALL_EVENT_TYPES
            if t.split(".", 1)[0] in events.DEFERRED_NAMESPACES
        }
        self.assertEqual(present, set(), "media.* and search.* are deferred (spec §3.4)")

    def test_catalogue_size_is_pinned(self):
        self.assertEqual(len(events.ALL_EVENT_TYPES), _EXPECTED_COUNT)

    def test_is_known_event_type(self):
        self.assertTrue(events.is_known_event_type(events.CLAIM_APPROVED))
        self.assertFalse(events.is_known_event_type("claim.approve"))
        self.assertFalse(events.is_known_event_type("media.processed"))
