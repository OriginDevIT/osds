"""The window pin (#245): it covers every limiter call site, and a flood that
straddles a window boundary on the real clock is not refused -- the failure this
helper exists to prevent."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.test import SimpleTestCase, TransactionTestCase

from audit import ratelimit
from audit.ratelimit import Rule
from audit.tests.window_clock import CALL_SITES, PINNED, pinned_windows
from tenants.models import Tenant

ROOT = Path(__file__).resolve().parents[2]
LIMITER = "audit.ratelimit"


def _dotted(node):
    """``a.b.c`` as "a.b.c" for a Name/Attribute chain, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def reaches_limiter_check(source):
    """True if the module imports ``check`` from audit.ratelimit by name, or
    calls ``check`` on a name bound to the audit.ratelimit module. Works on the
    syntax tree, so line breaks, parentheses, aliases and function-level
    imports make no difference."""
    tree = ast.parse(source)
    module_names = set()  # names bound to the audit.ratelimit module
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module == LIMITER and any(a.name == "check" for a in node.names):
                return True
            if node.module == "audit":
                module_names.update(a.asname or a.name for a in node.names if a.name == "ratelimit")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == LIMITER:
                    module_names.add(a.asname or LIMITER)
    return any(
        isinstance(node, ast.Attribute) and node.attr == "check" and _dotted(node.value) in module_names
        for node in ast.walk(tree)
    )


class CallSiteTests(SimpleTestCase):
    def test_guard_flags_every_way_of_reaching_check(self):
        planted = {
            "single-line import": "from audit.ratelimit import Rule, check\n",
            "parenthesised multi-line import": "from audit.ratelimit import (\n    Rule,\n    check,\n)\n",
            "aliased import": "from audit.ratelimit import check as limiter_check\n",
            "function-level import": "def f():\n    from audit.ratelimit import check\n",
            "module attribute": "from audit import ratelimit\n\nratelimit.check(None)\n",
            "aliased module attribute": "from audit import ratelimit as rl\n\nrl.check(None)\n",
            "dotted module import": "import audit.ratelimit as rl\n\nrl.check(None)\n",
            "full dotted path": "import audit.ratelimit\n\naudit.ratelimit.check(None)\n",
        }
        for label, source in planted.items():
            with self.subTest(label):
                self.assertTrue(reaches_limiter_check(source))

    def test_guard_ignores_unrelated_code(self):
        clean = {
            "other names from the module": "from audit.ratelimit import Rule, Verdict\n",
            "module imported, check unused": "from audit import ratelimit\n\nratelimit.Rule\n",
            "unrelated check": "from other import ratelimit\n\nratelimit.check(None)\n",
            "local check": "def check():\n    pass\n\ncheck()\n",
        }
        for label, source in clean.items():
            with self.subTest(label):
                self.assertFalse(reaches_limiter_check(source))

    def test_every_module_importing_check_is_pinned(self):
        found = set()
        for path in ROOT.rglob("*.py"):
            parts = path.relative_to(ROOT).parts
            if parts[0] in {".venv", "venv", "node_modules"} or "tests" in parts or "migrations" in parts:
                continue
            if path.relative_to(ROOT).as_posix() == "audit/ratelimit.py":
                continue
            if reaches_limiter_check(path.read_text(encoding="utf-8")):
                found.add(path.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".") + ".check")
        self.assertEqual(found, set(CALL_SITES))


class StraddleTests(TransactionTestCase):
    RULE = (Rule("t.straddle", 3, timedelta(minutes=1)),)

    def setUp(self):
        self.tenant = Tenant.objects.create(slug="acme", name="Acme", primary_domain="acme.test")

    def hit(self, now):
        return ratelimit.check(self.tenant, checks=[("ip", "203.0.113.5", self.RULE)], now=now)

    def test_a_flood_that_crosses_a_boundary_is_counted_in_two_windows(self):
        boundary = datetime(2026, 10, 4, 12, 1, 0, tzinfo=dt_timezone.utc)
        before, after = boundary - timedelta(milliseconds=1), boundary + timedelta(milliseconds=1)
        verdicts = [self.hit(before), self.hit(before), self.hit(after), self.hit(after), self.hit(after)]
        self.assertTrue(all(v.allowed for v in verdicts))  # five hits, limit three, none refused

    def test_pinned_the_same_flood_is_refused(self):
        from billing import inbound  # one of the pinned call sites

        with pinned_windows() as clock:
            verdicts = [
                inbound.check(self.tenant, checks=[("ip", "203.0.113.5", self.RULE)], now=PINNED + timedelta(hours=i))
                for i in range(5)  # the caller's clock is ignored
            ]
            self.assertEqual(clock.at, PINNED)
        self.assertEqual([v.allowed for v in verdicts], [True, True, True, False, False])
        self.assertEqual(verdicts[3].retry_after, 30)  # pinned 30s into the minute

    def test_advance_moves_to_a_new_window(self):
        from billing import inbound

        with pinned_windows() as clock:
            for _ in range(4):
                inbound.check(self.tenant, checks=[("ip", "203.0.113.5", self.RULE)], now=PINNED)
            clock.advance(minutes=2)
            verdict = inbound.check(self.tenant, checks=[("ip", "203.0.113.5", self.RULE)], now=PINNED)
        self.assertTrue(verdict.allowed)
