"""An unmatched path under admin/ must not reach the public dispatcher."""

from django.test import SimpleTestCase
from django.urls import Resolver404, resolve

URLCONF = "osds.urls_tenant"


class AdminCatchallTests(SimpleTestCase):
    def test_unmatched_admin_path_does_not_resolve_to_public_dispatch(self):
        for path in ("/admin/no-such-page/", "/admin/listings/no/such/", "/admin/x"):
            with self.subTest(path=path):
                try:
                    match = resolve(path, urlconf=URLCONF)
                except Resolver404:
                    continue
                self.assertNotEqual(match.url_name, "public-dispatch")

    def test_public_paths_still_reach_dispatcher(self):
        for path in ("/some-listing/", "/administrators/", "/adminx/y/"):
            with self.subTest(path=path):
                self.assertEqual(
                    resolve(path, urlconf=URLCONF).url_name, "public-dispatch"
                )

    def test_real_admin_routes_still_resolve(self):
        self.assertEqual(resolve("/admin/", urlconf=URLCONF).namespace, "directory_admin")
