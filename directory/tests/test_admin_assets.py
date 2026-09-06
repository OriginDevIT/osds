"""The admin surface loads its JS from a vendored file, never a CDN -- it runs
on an authenticated tenant session and a CDN load would leak the tenant domain
to a third party on every page.
"""

from __future__ import annotations

from django.contrib.staticfiles import finders
from django.template.loader import get_template
from django.test import SimpleTestCase

_CDN_HOSTS = ("unpkg.com", "jsdelivr", "cdnjs", "googleapis", "//cdn")


class VendoredHtmxTests(SimpleTestCase):
    def test_htmx_is_present_on_disk_with_a_provenance_header(self):
        path = finders.find("directory/vendor/htmx-2.0.4.min.js")
        self.assertIsNotNone(path, "vendored htmx not found by the static finder")
        with open(path, encoding="utf-8") as fh:
            head = fh.read(500)
        self.assertIn("vendored", head)
        self.assertIn("2.0.4", head)
        self.assertIn("bigskysoftware/htmx", head)

    def test_admin_base_template_references_the_vendored_file_only(self):
        source = get_template("directory/admin/base.html").template.source
        self.assertIn(
            "{% static 'directory/vendor/htmx-2.0.4.min.js' %}", source
        )
        for host in _CDN_HOSTS:
            self.assertNotIn(host, source)

    def test_no_admin_template_loads_a_cdn(self):
        import pathlib

        import directory

        admin_dir = pathlib.Path(directory.__file__).parent / "templates" / "directory" / "admin"
        for tpl in admin_dir.glob("*.html"):
            text = tpl.read_text(encoding="utf-8")
            for host in _CDN_HOSTS:
                self.assertNotIn(host, text, f"{tpl.name} references {host}")
