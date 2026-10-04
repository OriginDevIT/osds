"""The boundary: ``adapters/stripe`` talks to core only through the published
contract (CLAUDE.md invariant 1 and the adapter rule in "Repository layout")."""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

PACKAGE = Path(__file__).resolve().parent.parent / "stripe"
FORBIDDEN = {"billing", "directory", "tenants", "audit"}
ALLOWED_CORE = {"osds.adapter_api"}
# Third-party and stdlib are fine; these are the project's own top-level packages.
PROJECT = FORBIDDEN | {"osds", "adapters"}


def imported_modules(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            yield ("." * node.level) + (node.module or "")
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "__import__":
            yield "__import__"
        elif (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "import_module"):
            yield "import_module"


class BoundaryTests(SimpleTestCase):
    def files(self):
        files = sorted(PACKAGE.glob("*.py"))
        self.assertTrue(files, "adapters/stripe has no modules?")
        return files

    def test_no_import_reaches_core_internals(self):
        for path in self.files():
            for module in imported_modules(path):
                with self.subTest(file=path.name, module=module):
                    self.assertNotIn(module.split(".")[0], FORBIDDEN)
                    self.assertFalse(module.startswith("."), "relative imports are not used here")
                    self.assertNotIn(module, ("__import__", "import_module"))

    def test_the_only_core_import_is_the_published_contract(self):
        for path in self.files():
            for module in imported_modules(path):
                if module.split(".")[0] == "osds":
                    allowed = ALLOWED_CORE | ({"osds.adapters"} if path.name == "apps.py" else set())
                    with self.subTest(file=path.name, module=module):
                        self.assertIn(module, allowed)

    def test_apps_py_is_the_only_file_that_touches_the_registry(self):
        for path in self.files():
            with self.subTest(file=path.name):
                if path.name != "apps.py":
                    self.assertNotIn("osds.adapters", set(imported_modules(path)))

    def test_no_network_library_beyond_the_stdlib_and_no_sdk(self):
        for path in self.files():
            for module in imported_modules(path):
                with self.subTest(file=path.name, module=module):
                    self.assertNotIn(module.split(".")[0], {"stripe", "requests", "httpx", "urllib3"})

    def test_it_never_opens_a_socket_itself(self):
        for path in self.files():
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertNotIn("urlopen", text)
                self.assertNotIn("urllib.request", text)
                self.assertNotIn("http.client", text)
