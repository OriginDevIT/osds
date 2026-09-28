"""Core never imports adapter code (CLAUDE.md invariant 1, decisions.md §4.3:
"A test asserts nothing under directory/, tenants/, billing/ or audit/
imports adapters").

Generalises ``audit/tests/test_drain.py``'s ``WorkerSourceInvariantTests``
(which held only ``audit/worker/`` to this rule) to every core app now that
an adapter package -- ``adapters.smtp`` -- actually exists to import.
"""

from __future__ import annotations

import ast
import pathlib

from django.test import SimpleTestCase

import audit
import billing
import directory
import tenants

_CORE_PACKAGES = [audit, billing, directory, tenants]


class CoreImportsNoAdapterCodeTests(SimpleTestCase):
    def _sources(self):
        for package in _CORE_PACKAGES:
            root = pathlib.Path(package.__file__).parent
            yield from sorted(root.rglob("*.py"))

    def test_nothing_under_the_four_core_apps_imports_adapters(self):
        for path in self._sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotEqual(
                            alias.name.split(".")[0],
                            "adapters",
                            f"{path} imports adapter code",
                        )
                elif isinstance(node, ast.ImportFrom):
                    head = (node.module or "").split(".")[0]
                    self.assertNotEqual(
                        head, "adapters", f"{path} imports adapter code"
                    )

    def test_the_check_actually_scans_a_nontrivial_number_of_files(self):
        # Guard against a refactor (or an empty package list) making the
        # assertion above vacuously true.
        self.assertGreater(len(list(self._sources())), 50)
