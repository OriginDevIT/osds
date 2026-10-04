"""Core never imports adapter code (CLAUDE.md invariant 1, decisions.md §4.3:
"A test asserts nothing under directory/, tenants/, billing/ or audit/
imports adapters").

Generalises ``audit/tests/test_drain.py``'s ``WorkerSourceInvariantTests``
(which held only ``audit/worker/`` to this rule) to every core app now that
an adapter package -- ``adapters.smtp`` -- actually exists to import.
"""

from __future__ import annotations

import ast
import importlib
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


class AdaptersImportOnlyTheContractTests(SimpleTestCase):
    """#215, spec §8.3: an adapter never touches core. Everything under
    ``adapters/`` -- the SMTP sender and the webhook adapter included -- imports
    ``osds.adapter_api`` and ``osds.adapters`` from the project and nothing else:
    it is handed an ``AdapterContext`` and plain data, and returns plain data.
    """

    FORBIDDEN = {"tenants", "directory", "billing", "audit"}
    # The published contract: the only part of ``osds`` an adapter may import.
    CONTRACT = {"osds.adapter_api", "osds.adapters"}

    def _sources(self):
        # importlib, not an import statement: the sibling test above scans this
        # very file for imports of adapter code.
        adapters = importlib.import_module("adapters")
        root = pathlib.Path(adapters.__file__).parent
        for path in sorted(root.rglob("*.py")):
            if "tests" in path.parts:
                continue
            yield path

    def _imports(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield alias.name
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                yield node.module or ""

    def test_nothing_under_adapters_imports_core(self):
        for path in self._sources():
            for module in self._imports(path):
                self.assertNotIn(module.split(".")[0], self.FORBIDDEN, f"{path} imports {module}")

    def test_the_only_part_of_osds_an_adapter_imports_is_the_contract(self):
        for path in self._sources():
            for module in self._imports(path):
                if module.split(".")[0] == "osds":
                    self.assertIn(module, self.CONTRACT, f"{path} imports {module}")

    def test_the_check_scans_the_real_adapters(self):
        names = {p.name for p in self._sources()}
        self.assertTrue({"sender.py", "adapter.py", "signing.py"} <= names, names)
