"""Assertions shared by the per-page settings command-log tests (#225)."""

from __future__ import annotations

import json

from audit.models import CommandLog


def settings_rows(tenant) -> "list[CommandLog]":
    return list(
        CommandLog.all_tenants.filter(command="settings.update", tenant=tenant).order_by("id")
    )


def assert_one_save(case, tenant, operator, *, page, fields, op="replace", forbidden=(), since=0):
    """Exactly one new ``settings.update`` row since ``since`` rows existed: applied,
    actor the operator, payload the page and the field names, and none of the
    ``forbidden`` submitted values anywhere in the row."""
    rows = settings_rows(tenant)[since:]
    case.assertEqual(len(rows), 1, [r.payload for r in rows])
    row = rows[0]
    case.assertEqual(row.outcome, "applied")
    case.assertIsNotNone(row.concluded_at)
    case.assertEqual(row.actor, {"type": "admin", "id": operator.public_id})
    case.assertEqual(row.payload, {"page": page, "op": op, "fields": sorted(fields)})
    blob = json.dumps([row.payload, row.problem, row.actor, row.adapter_id])
    for value in forbidden:
        case.assertNotIn(value, blob)
    return row
