"""audit.redaction (invariant 8, #238): what an adapter may see.

Pure tests -- no database. The classification tests are the drift guard: they
fail when an event is emitted that nobody has decided about.
"""

from __future__ import annotations

import ast
import json
import pathlib
from itertools import combinations

from django.test import SimpleTestCase

import audit
from audit import events
from audit.redaction import (
    CONTACT, MESSAGE, NO_PII, PATCH_RULES, RULES, SCOPES, classified, redact,
)

NONE = frozenset()
BOTH = frozenset(SCOPES)


def env(event_type, data, **extra):
    return {
        "id": "01TESTEVENT", "type": event_type, "version": 1, "subject": "subj_1",
        "actor": {"type": "visitor", "id": ""}, "origin": None, "data": data, **extra,
    }


LEAD = {
    "lead": {
        "id": "lead_1", "kind": "contact_form", "name": "Priya R.",
        "email": "priya@example.test", "phone_e164": "+13125550188",
        "message": "Burst pipe, need someone today.", "spam_score": 0.02,
    },
    "listing_id": "listing_1",
    "source_page": "/plumbers/hoffman",
    "consent": {"contact_by_business": {"granted": True, "ip": "198.51.100.7", "text_version": "lead-consent-v1"}},
}


class LeadTests(SimpleTestCase):
    def test_with_no_scope_every_personal_value_is_withheld_and_the_rest_survives(self):
        out = redact(env("lead.captured", LEAD), granted=NONE)
        lead = out["data"]["lead"]
        self.assertEqual(
            (lead["name"], lead["email"], lead["phone_e164"], lead["message"]),
            (None, None, None, None),
        )
        self.assertEqual((lead["id"], lead["kind"], lead["spam_score"]), ("lead_1", "contact_form", 0.02))
        self.assertIsNone(out["data"]["consent"]["contact_by_business"]["ip"])
        self.assertEqual(out["data"]["consent"]["contact_by_business"]["text_version"], "lead-consent-v1")
        self.assertEqual(out["data"]["listing_id"], "listing_1")
        self.assertEqual(
            out["redacted"],
            ["consent.contact_by_business.ip", "lead.email", "lead.message", "lead.name", "lead.phone_e164"],
        )

    def test_contact_alone_leaves_the_message_withheld(self):
        out = redact(env("lead.captured", LEAD), granted={CONTACT})
        self.assertEqual(out["data"]["lead"]["email"], "priya@example.test")
        self.assertIsNone(out["data"]["lead"]["message"])
        self.assertEqual(out["redacted"], ["lead.message"])

    def test_message_alone_leaves_the_contact_withheld(self):
        out = redact(env("lead.captured", LEAD), granted={MESSAGE})
        self.assertEqual(out["data"]["lead"]["message"], "Burst pipe, need someone today.")
        self.assertIsNone(out["data"]["lead"]["email"])

    def test_both_scopes_withhold_nothing(self):
        out = redact(env("lead.captured", LEAD), granted=BOTH)
        self.assertEqual(out["data"], LEAD)
        self.assertEqual(out["redacted"], [])

    def test_the_input_is_never_mutated(self):
        original = env("lead.captured", LEAD)
        snapshot = json.dumps(original, sort_keys=True)
        redact(original, granted=NONE)
        self.assertEqual(json.dumps(original, sort_keys=True), snapshot)

    def test_an_unknown_scope_grants_nothing(self):
        out = redact(env("lead.captured", LEAD), granted={"pii:everything"})
        self.assertIsNone(out["data"]["lead"]["email"])

    def test_redacted_is_always_present(self):
        self.assertEqual(redact(env("lead.marked_spam", {"lead_id": "x"}))["redacted"], [])


class OtherEventTests(SimpleTestCase):
    def test_user_created(self):
        data = {"user": {"id": "usr_1", "email": "a@x.test", "name": "A", "phone_e164": "+1"}, "created_by": "claim.submit"}
        out = redact(env("user.created", data))["data"]
        self.assertEqual(out["user"], {"id": "usr_1", "email": None, "name": None, "phone_e164": None})
        self.assertEqual(out["created_by"], "claim.submit")

    def test_claim_submitted(self):
        data = {
            "claim": {"id": "claim_1"},
            "claimant": {"id": "usr_1", "name": "D", "email": "d@x.test", "phone_e164": "+1", "role_claimed": "owner"},
            "consent": {"marketing_email": {"granted": True, "ip": "1.2.3.4"}, "marketing_sms": {"granted": False, "ip": None}},
        }
        out = redact(env("claim.submitted", data))
        claimant = out["data"]["claimant"]
        self.assertEqual((claimant["name"], claimant["email"], claimant["phone_e164"]), (None, None, None))
        self.assertEqual((claimant["id"], claimant["role_claimed"]), ("usr_1", "owner"))
        self.assertIsNone(out["data"]["consent"]["marketing_email"]["ip"])
        self.assertIn("consent.marketing_sms.ip", out["redacted"])

    def test_staff_invited(self):
        out = redact(env("staff.invited", {"operator": {"id": "op_1", "email": "o@x.test", "existing": False}}))
        self.assertIsNone(out["data"]["operator"]["email"])
        self.assertEqual(out["data"]["operator"]["id"], "op_1")

    def test_listing_created_withholds_the_whole_contact_block(self):
        data = {"slug": "s", "name": "Hoffman", "contact": {"phone_e164": "+1", "email": "o@x.test", "website": "https://x.test", "social": []}, "type": "business"}
        out = redact(env("listing.created", data))
        self.assertIsNone(out["data"]["contact"])
        self.assertEqual((out["data"]["name"], out["data"]["slug"]), ("Hoffman", "s"))
        self.assertEqual(redact(env("listing.created", data), granted={CONTACT})["data"]["contact"]["email"], "o@x.test")

    def test_free_text_fields_need_the_message_scope(self):
        cases = [
            ("claim.rejected", {"claim": {"id": "c"}, "reason": "no evidence"}, "reason"),
            ("moderation.decided", {"item_id": "m", "rationale": "looks fine"}, "rationale"),
            ("billing.subscription_canceled", {"listing_id": "l", "reason": "too dear"}, "reason"),
            ("entitlement.canceled", {"entitlement_id": "e", "reason": "moved"}, "reason"),
            ("entitlement.overridden", {"entitlement_id": "e", "reason": "comp", "admin_id": "op"}, "reason"),
        ]
        for event_type, data, key in cases:
            with self.subTest(event_type=event_type):
                self.assertIsNone(redact(env(event_type, data))["data"][key])
                self.assertIsNone(redact(env(event_type, data), granted={CONTACT})["data"][key])
                self.assertIsNotNone(redact(env(event_type, data), granted={MESSAGE})["data"][key])

    def test_claim_approved_verification_notes(self):
        data = {"claim": {"id": "c"}, "manual_verification": {"method_used": "phone", "notes": "called", "evidence_ref": "doc", "verified_by": "op"}}
        out = redact(env("claim.approved", data))["data"]["manual_verification"]
        self.assertEqual((out["notes"], out["evidence_ref"], out["method_used"]), (None, None, "phone"))

    def test_import_errors_can_quote_values(self):
        out = redact(env("import.completed", {"batch_id": "b", "errors": [{"row": 3, "error": "bad phone +1555"}], "created": 2}))
        self.assertIsNone(out["data"]["errors"])
        self.assertEqual(out["data"]["created"], 2)


class PatchTests(SimpleTestCase):
    def changes(self, *ops):
        return env("listing.updated", {"type": "business", "changes": [dict(op) for op in ops]})

    def test_contact_ops_keep_their_path_and_lose_their_value(self):
        out = redact(self.changes(
            {"op": "replace", "path": "/contact/phone_e164", "value": "+1555"},
            {"op": "replace", "path": "/contact", "value": {"email": "o@x.test"}},
            {"op": "replace", "path": "/name", "value": "New Name"},
        ))
        a, b, c = out["data"]["changes"]
        self.assertEqual((a["op"], a["path"], a["value"]), ("replace", "/contact/phone_e164", None))
        self.assertEqual((b["path"], b["value"]), ("/contact", None))
        self.assertEqual(c["value"], "New Name")
        self.assertEqual(out["redacted"], ["changes[0].value", "changes[1].value"])

    def test_contact_scope_releases_them(self):
        out = redact(self.changes({"op": "replace", "path": "/contact/email", "value": "o@x.test"}), granted={CONTACT})
        self.assertEqual(out["data"]["changes"][0]["value"], "o@x.test")

    def test_a_path_that_only_starts_with_contact_is_not_the_contact_block(self):
        out = redact(self.changes({"op": "replace", "path": "/contactless", "value": "x"}))
        self.assertEqual(out["data"]["changes"][0]["value"], "x")

    def test_a_remove_op_has_no_value_to_withhold(self):
        out = redact(self.changes({"op": "remove", "path": "/contact/email"}))
        self.assertEqual(out["data"]["changes"][0], {"op": "remove", "path": "/contact/email"})

    def test_tenant_settings_values_are_all_withheld(self):
        out = redact(env("tenant.settings_changed", {"changed_by": "op", "changes": [{"op": "add", "path": "/smtp", "value": {"host": "mail.x"}}]}))
        self.assertIsNone(out["data"]["changes"][0]["value"])
        self.assertEqual(out["data"]["changed_by"], "op")


class PrivateCustomFieldTests(SimpleTestCase):
    PRIVATE = frozenset({"license"})

    def test_listing_created(self):
        data = {"type": "business", "contact": {}, "custom_fields": {"license": "LIC-9", "hours": "9-5"}}
        out = redact(env("listing.created", data), private_custom_fields=self.PRIVATE)
        self.assertEqual(out["data"]["custom_fields"], {"license": None, "hours": "9-5"})
        self.assertIn("custom_fields.license", out["redacted"])

    def test_granted_contact_releases_them(self):
        data = {"type": "business", "contact": {}, "custom_fields": {"license": "LIC-9"}}
        out = redact(env("listing.created", data), granted={CONTACT}, private_custom_fields=self.PRIVATE)
        self.assertEqual(out["data"]["custom_fields"]["license"], "LIC-9")

    def test_patch_ops(self):
        event = env("listing.updated", {"type": "business", "changes": [
            {"op": "replace", "path": "/custom_fields/license", "value": "LIC-10"},
            {"op": "replace", "path": "/custom_fields/hours", "value": "8-4"},
            {"op": "replace", "path": "/custom_fields", "value": {"license": "LIC-11", "hours": "7-3"}},
        ]})
        out = redact(event, private_custom_fields=self.PRIVATE)["data"]["changes"]
        self.assertIsNone(out[0]["value"])
        self.assertEqual(out[1]["value"], "8-4")
        self.assertEqual(out[2]["value"], {"license": None, "hours": "7-3"})

    def test_message_scope_does_not_release_them(self):
        data = {"type": "business", "contact": {}, "custom_fields": {"license": "LIC-9"}}
        out = redact(env("listing.created", data), granted={MESSAGE}, private_custom_fields=self.PRIVATE)
        self.assertIsNone(out["data"]["custom_fields"]["license"])


class FailClosedTests(SimpleTestCase):
    def test_an_unclassified_event_is_withheld_in_full(self):
        out = redact(env("review.submitted", {"reviewer": "Pat", "body": "great"}))
        self.assertEqual(out["data"], {})
        self.assertEqual(out["redacted"], ["data"])

    def test_one_scope_is_not_enough_to_see_an_unclassified_event(self):
        self.assertEqual(redact(env("review.submitted", {"body": "x"}), granted={CONTACT})["data"], {})
        self.assertEqual(redact(env("review.submitted", {"body": "x"}), granted={MESSAGE})["data"], {})

    def test_both_scopes_see_it(self):
        self.assertEqual(redact(env("review.submitted", {"body": "x"}), granted=BOTH)["data"], {"body": "x"})

    def test_a_reviewed_event_passes_untouched(self):
        out = redact(env("entitlement.started", {"entitlement_id": "e", "tier": "featured"}))
        self.assertEqual(out["data"], {"entitlement_id": "e", "tier": "featured"})
        self.assertEqual(out["redacted"], [])


# --- the drift guard --------------------------------------------------------------------


def _core_sources():
    root = pathlib.Path(audit.__file__).parent.parent
    for package in ("directory", "tenants", "billing", "audit", "osds"):
        for path in sorted((root / package).rglob("*.py")):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            yield root, path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


_CONSTANTS = {
    name: value for name, value in vars(events).items() if name.isupper() and isinstance(value, str)
}
_PII_KEYS = {"email", "phone_e164", "name", "message", "ip", "notes", "reason", "rationale", "errors", "evidence_ref"}
# Reviewed: a key that looks personal and is not. (event, dotted path) -> why.
REVIEWED_NOT_PII = {
    ("listing.unpublished", "reason"): "a system reason code (downgrade, expiry), never typed by a person",
    ("claim.verification_failed", "reason"): "one of wrong_code, cooldown, locked, ineligible: fixed codes",
}


def _emitted():
    """(event type, set of dotted literal data keys) for every emit() call."""
    found = {}
    for _, _, tree in _core_sources():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            first = node.args[0]
            if name != "emit" or not isinstance(first, ast.Attribute) or first.attr not in _CONSTANTS:
                continue
            keys = found.setdefault(_CONSTANTS[first.attr], set())
            for kw in node.keywords:
                if kw.arg == "data":
                    _collect(kw.value, keys, "")
    return found


def _collect(node, out, prefix):
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                out.add(prefix + key.value)
                _collect(value, out, prefix + key.value + ".")
    elif isinstance(node, (ast.List, ast.Tuple)):
        for item in node.elts:
            _collect(item, out, prefix)


def _covered(event_type, path):
    for rule_path, _ in RULES.get(event_type, ()):
        rule, got = rule_path.split("."), path.split(".")
        if len(got) >= len(rule) and all(r in ("*", g) for r, g in zip(rule, got)):
            return True
    return False


class ClassificationTests(SimpleTestCase):
    def test_every_classified_type_is_a_real_event_type(self):
        self.assertLessEqual(set(RULES) | NO_PII, set(events.ALL_EVENT_TYPES))

    def test_nothing_is_both_ruled_and_reviewed_clean(self):
        self.assertEqual(set(RULES) & NO_PII, set())

    def test_every_patch_event_has_rules_entry(self):
        self.assertLessEqual(set(PATCH_RULES), set(RULES))

    def test_every_emitted_event_type_is_classified(self):
        unclassified = sorted(t for t in _emitted() if not classified(t))
        self.assertEqual(
            unclassified, [],
            "These events are emitted but not classified in audit.redaction: add each "
            "to RULES (with the paths that carry personal data) or to NO_PII after "
            "reading its payload.",
        )

    def test_every_personal_looking_payload_key_is_covered_or_reviewed(self):
        uncovered = []
        for event_type, keys in sorted(_emitted().items()):
            for path in sorted(keys):
                if path.rsplit(".", 1)[-1] not in _PII_KEYS:
                    continue
                if _covered(event_type, path) or (event_type, path) in REVIEWED_NOT_PII:
                    continue
                uncovered.append(f"{event_type}: {path}")
        self.assertEqual(
            uncovered, [],
            "A payload key that looks personal is neither redacted nor reviewed. Add a "
            "RULES path, or a REVIEWED_NOT_PII entry saying why it is safe.",
        )

    def test_envelopes_reach_a_subscriber_through_exactly_one_call_site(self):
        callers = []
        for root, path, tree in _core_sources():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                    if name == "to_wire":
                        callers.append(str(path.relative_to(root)).replace("\\", "/"))
        self.assertEqual(callers, ["audit/worker/drain.py"])


def _populate(rules):
    """A payload with a sentinel at every rule path."""
    data = {"control": "KEEP"}
    for path, scope in rules:
        node, parts = data, [("x" if p == "*" else p) for p in path.split(".")]
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = f"SENTINEL|{scope}|{path}"
    return data


class SentinelTests(SimpleTestCase):
    def test_no_sentinel_survives_without_its_scope_and_all_survive_with_it(self):
        for event_type, rules in RULES.items():
            if not rules:
                continue
            data = _populate(rules)
            for size in range(0, 3):
                for granted in map(frozenset, combinations(SCOPES, size)):
                    with self.subTest(event_type=event_type, granted=sorted(granted)):
                        out = json.dumps(redact(env(event_type, data), granted=granted)["data"])
                        for scope in {s for _, s in rules}:
                            marker = f"SENTINEL|{scope}|"
                            self.assertEqual(marker in out, scope in granted)
                        self.assertIn("KEEP", out)
