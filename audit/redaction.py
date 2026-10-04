"""Redacting an envelope before an adapter sees it (CLAUDE.md invariant 8,
#238, decisions.md §4.12).

**PII is opt-in per adapter; redaction is the default.** The drain calls
``redact`` on every wire envelope, in core, between ``to_wire`` and
``subscriber.handle``. An adapter never receives the unredacted envelope and has
nothing to forget to do. What it receives depends on ``granted``: the scopes its
tenant's admin has turned on, out of the scopes it asked for
(``osds.adapter_context.granted_scopes``).

Two scopes (spec §8):

* ``pii:contact`` -- who someone is and how to reach them: names, email
  addresses, phone numbers, IP addresses, a listing's contact block, and a
  listing type's custom fields the operator marked non-public.
* ``pii:message`` -- free text that someone wrote: an inquiry message, and the
  notes and reasons staff and owners type.

A withheld value becomes ``null`` where it was, and the envelope gains
``redacted``: the paths withheld. An adapter can tell "withheld" from "absent".

**Fail closed.** ``RULES`` classifies every event type that carries personal
data; ``NO_PII`` lists those reviewed and found to carry none. An event type in
neither is withheld in full from a subscriber that does not hold both scopes.
Tests fail if an emitted type is unclassified (``audit/tests/test_redaction``),
so adding an event forces the question to be answered.

Paths are dotted, ``*`` matches any key at its level. JSON-patch events
(``listing.updated``, ``tenant.settings_changed``) are handled op by op: the op
stays, so a consumer sees that something changed, but its ``value`` is withheld.
"""

from __future__ import annotations

import copy

CONTACT = "pii:contact"
MESSAGE = "pii:message"
SCOPES = (CONTACT, MESSAGE)

# event type -> (dotted path, scope) the path needs. Absent paths are skipped.
RULES: "dict[str, tuple[tuple[str, str], ...]]" = {
    "user.created": (
        ("user.email", CONTACT), ("user.name", CONTACT), ("user.phone_e164", CONTACT),
    ),
    "claim.submitted": (
        ("claimant.name", CONTACT), ("claimant.email", CONTACT),
        ("claimant.phone_e164", CONTACT), ("consent.*.ip", CONTACT),
    ),
    "lead.captured": (
        ("lead.name", CONTACT), ("lead.email", CONTACT), ("lead.phone_e164", CONTACT),
        ("lead.message", MESSAGE), ("consent.*.ip", CONTACT),
    ),
    "listing.created": (("contact", CONTACT),),
    "listing.updated": (),  # JSON patch, below
    "staff.invited": (("operator.email", CONTACT),),
    "claim.approved": (
        ("manual_verification.notes", MESSAGE),
        ("manual_verification.evidence_ref", MESSAGE),
    ),
    "claim.rejected": (("reason", MESSAGE),),
    "moderation.decided": (("rationale", MESSAGE),),
    "billing.subscription_canceled": (("reason", MESSAGE),),
    "entitlement.canceled": (("reason", MESSAGE),),
    "entitlement.overridden": (("reason", MESSAGE),),
    # Row errors can quote the offending value (a phone number, a name).
    "import.completed": (("errors", CONTACT),),
    "tenant.settings_changed": (),  # JSON patch, below
}

# Events whose ``data.changes`` is a JSON patch -> [(path prefix, scope)]. A
# prefix of "/" means every op. An op under a prefix keeps its ``path`` and ``op``
# and loses its ``value``.
PATCH_RULES: "dict[str, tuple[tuple[str, str], ...]]" = {
    "listing.updated": (("/contact", CONTACT),),
    # Values here are installation configuration: a mail host, a from address, a
    # webhook's host. Nothing an adapter acting for a directory needs.
    "tenant.settings_changed": (("/", CONTACT),),
}

# Reviewed and found to carry no personal data. Explicit, never derived from
# ALL_EVENT_TYPES: a new event must be looked at, not inherit a free pass.
NO_PII: "frozenset[str]" = frozenset(
    {
        "claim.verification_started",  # the destination is already masked
        "claim.verification_failed",
        "claim.disputed",
        "claim.notified_existing_contacts",  # channels, destinations masked
        "listing.owner_assigned",
        "listing.published",
        "listing.unpublished",
        "listing.tier_changed",
        "staff.accepted",
        "lead.marked_spam",
        "moderation.queued",
        "tenant.created",
        "tenant.domain_verified",
        "import.started",
        "import.rolled_back",
        "billing.checkout_started",
        "billing.subscription_started",
        "billing.subscription_changed",
        "billing.payment_succeeded",
        "billing.payment_failed",
        "billing.refund_issued",
        "entitlement.started",
        "entitlement.trial_converted",
        "entitlement.dunning_started",
        "entitlement.recovered",
        "entitlement.downgraded",
        "entitlement.restored",
        "entitlement.renewal_due",
        "entitlement.expired",
    }
)

LISTING_EVENTS = frozenset({"listing.created", "listing.updated"})


def classified(event_type: str) -> bool:
    return event_type in RULES or event_type in NO_PII


def _null_path(node, parts, redacted, trail) -> None:
    key, rest = parts[0], parts[1:]
    if not isinstance(node, dict):
        return
    keys = list(node) if key == "*" else ([key] if key in node else [])
    for k in keys:
        path = f"{trail}{k}"
        if rest:
            _null_path(node[k], rest, redacted, path + ".")
        else:
            node[k] = None
            redacted.append(path)


def _under(path: str, prefix: str) -> bool:
    return prefix == "/" or path == prefix or path.startswith(prefix + "/")


def _redact_patch(changes, rules, granted, private_custom_fields, redacted) -> None:
    if not isinstance(changes, list):
        return
    for index, op in enumerate(changes):
        if not isinstance(op, dict) or "value" not in op:
            continue
        path = str(op.get("path", ""))
        withheld = any(_under(path, prefix) and scope not in granted for prefix, scope in rules)
        if not withheld and CONTACT not in granted and private_custom_fields:
            if path == "/custom_fields" and isinstance(op["value"], dict):
                for key in private_custom_fields & set(op["value"]):
                    op["value"][key] = None
                    redacted.append(f"changes[{index}].value.{key}")
            elif path.startswith("/custom_fields/"):
                if path.split("/")[2] in private_custom_fields:
                    withheld = True
        if withheld:
            op["value"] = None
            redacted.append(f"changes[{index}].value")


def redact(
    envelope: dict, *, granted=frozenset(), private_custom_fields=frozenset()
) -> dict:
    """A copy of ``envelope`` with everything ``granted`` does not cover
    withheld, and a ``redacted`` list of the paths that were. ``private_custom_fields``
    are the keys of a listing type's custom fields the operator marked
    non-public; they need ``pii:contact``."""
    granted = frozenset(granted)
    event_type = envelope.get("type", "")
    data = copy.deepcopy(envelope.get("data") or {})
    redacted: "list[str]" = []

    if event_type in RULES:
        for path, scope in RULES[event_type]:
            if scope not in granted:
                _null_path(data, path.split("."), redacted, "")
        if event_type in PATCH_RULES:
            _redact_patch(
                data.get("changes"), PATCH_RULES[event_type], granted,
                frozenset(private_custom_fields), redacted,
            )
    elif event_type not in NO_PII and not set(SCOPES) <= granted:
        data = {}
        redacted.append("data")

    if event_type in LISTING_EVENTS and CONTACT not in granted and private_custom_fields:
        fields = data.get("custom_fields")
        if isinstance(fields, dict):
            for key in frozenset(private_custom_fields) & set(fields):
                fields[key] = None
                redacted.append(f"custom_fields.{key}")

    out = dict(envelope)
    out["data"] = data
    out["redacted"] = sorted(set(redacted))
    return out
