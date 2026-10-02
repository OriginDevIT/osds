"""Architectural invariants for money (CLAUDE.md invariants 1 and 2).

* There is no code path that sets a listing's tier directly: only
  ``billing.entitlements._set_tier`` assigns ``Listing.current_tier``.
* Core knows capability names, never a vendor's: no payment vendor appears in
  ``directory/``, ``tenants/``, ``billing/`` or ``audit/``.
"""

from __future__ import annotations

import ast
import pathlib
import re

from django.test import SimpleTestCase

import audit
import billing
import directory
import osds
import tenants

CORE = (audit, billing, directory, osds, tenants)
# "square" is left out on purpose: it is an ordinary word, and ``square.site`` is a
# free-hosted-site domain in ``directory.claims.PLATFORM_HOSTS``, data and not an
# integration.
VENDORS = re.compile(
    r"(?<![a-z])(stripe|paypal|braintree|adyen|gohighlevel|twilio)(?![a-z])", re.I
)

_READ_CALLS = frozenset(
    {"filter", "exclude", "get", "annotate", "select_related", "prefetch_related",
     "order_by", "values", "values_list", "count", "exists", "first", "last",
     "aggregate", "in_bulk"}
)


def _walk_with_scope(tree):
    stack = [(None, tree)]
    while stack:
        scope, node = stack.pop()
        yield scope, node
        child = node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
        for c in ast.iter_child_nodes(node):
            stack.append((child, c))


def tier_writes(source: str, filename: str = "<src>") -> list:
    """``(function, line, name)`` for every assignment or keyword that sets
    ``current_tier`` / ``current_tier_id``, except reads and a ``request``'s
    own attributes."""
    hits, exempt = [], set()
    for scope, node in _walk_with_scope(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in _READ_CALLS:
                exempt.update(id(k) for k in node.keywords)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr in ("current_tier", "current_tier_id"):
                    hits.append((scope, node.lineno, t.attr))
        elif isinstance(node, ast.keyword) and node.arg in ("current_tier", "current_tier_id") and id(node) not in exempt:
            hits.append((scope, getattr(node, "lineno", 0), node.arg))
    return hits


def _sources():
    for package in CORE:
        root = pathlib.Path(package.__file__).parent
        for path in sorted(root.rglob("*.py")):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            yield package, path


class TierWriterTests(SimpleTestCase):
    def test_only__set_tier_assigns_current_tier(self):
        offenders = []
        for _, path in _sources():
            for scope, lineno, name in tier_writes(path.read_text(encoding="utf-8"), str(path)):
                if path.name == "entitlements.py" and scope == "_set_tier":
                    continue
                offenders.append(f"{path.name}:{lineno} {name}")
        self.assertEqual(offenders, [])

    def test_the_scanner_catches_every_kind_of_write(self):
        writes = {
            "attribute": "def f(l, t):\n    l.current_tier = t\n",
            "id": "def f(l, t):\n    l.current_tier_id = t\n",
            "update": "def f(t):\n    Listing.objects.filter(pk=1).update(current_tier=t)\n",
            "create": "def f(t):\n    Listing.objects.create(current_tier=t)\n",
            "constructor": "def f(t):\n    return Listing(current_tier=t)\n",
        }
        for label, source in writes.items():
            with self.subTest(label):
                self.assertEqual(len(tier_writes(source)), 1)

    def test_the_scanner_ignores_reads(self):
        for source in (
            "def f(t):\n    return Listing.objects.filter(current_tier=t)\n",
            "def f(t):\n    return Listing.objects.exclude(current_tier=t).get(pk=1)\n",
        ):
            self.assertEqual(tier_writes(source), [])

    def test_the_scan_covers_the_real_writer(self):
        found = [
            (p.name, scope)
            for _, p in _sources()
            for scope, _, _ in tier_writes(p.read_text(encoding="utf-8"))
        ]
        self.assertIn(("entitlements.py", "_set_tier"), found)


class NoVendorInCoreTests(SimpleTestCase):
    def test_no_payment_vendor_is_named_in_core(self):
        offenders = []
        for _, path in _sources():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if VENDORS.search(line):
                    offenders.append(f"{path.parent.name}/{path.name}:{n}: {line.strip()[:70]}")
        self.assertEqual(offenders, [])

    def test_the_pattern_would_catch_one_and_spare_look_alikes(self):
        for hit in ("from adapters.stripe import x", "stripe_customer_id = models.CharField()",
                    "customer = Stripe(key)", "STRIPE_KEY", "paypal2"):
            self.assertTrue(VENDORS.search(hit), hit)
        for miss in ("a free-hosted site on squarespace.com", "stripes", "a squared grid", "square.site"):
            self.assertIsNone(VENDORS.search(miss), miss)
