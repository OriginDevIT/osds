"""Issue #122 -- tenant URLs built outside a request.

``reverse()`` needs a urlconf on the thread-local, which only a request sets.
Non-request code (the worker, management commands, migrations, the future
sitemap generator) builds URL strings with ``directory.routing`` instead.

These tests run with no test client, so ``set_urlconf`` is never called for
them -- that is the whole point. A test that goes through ``Client`` has
already had the urlconf set for it and proves nothing here.
"""

from __future__ import annotations

import inspect
from importlib import import_module

from django.test import SimpleTestCase
from django.urls import NoReverseMatch, reverse, set_urlconf
from django.utils import timezone

from directory import routing
from osds import middleware, urlconf
from tenants.models import Tenant


class AbsoluteUrlTests(SimpleTestCase):
    """``routing.absolute_url`` -- the one implementation of the
    https / ``domain_verified_at`` rule, callable with no request."""

    def test_relative_until_the_domain_is_verified(self):
        tenant = Tenant(slug="acme", name="Acme", primary_domain="acme.test")
        self.assertEqual(routing.absolute_url(tenant, "/plumbers/x"), "/plumbers/x")

    def test_absolute_https_once_the_domain_is_verified(self):
        tenant = Tenant(
            slug="acme",
            name="Acme",
            primary_domain="acme.test",
            domain_verified_at=timezone.now(),
        )
        self.assertEqual(
            routing.absolute_url(tenant, "/plumbers/x"),
            "https://acme.test/plumbers/x",
        )

    def test_no_domain_stays_relative(self):
        tenant = Tenant(slug="acme", name="Acme")
        self.assertEqual(routing.absolute_url(tenant, "/x"), "/x")

    def test_scheme_does_not_depend_on_debug_or_secure_cookies(self):
        tenant = Tenant(
            slug="acme",
            name="Acme",
            primary_domain="acme.test",
            domain_verified_at=timezone.now(),
        )
        with self.settings(DEBUG=True, OSDS_SECURE_COOKIES=False):
            self.assertTrue(
                routing.absolute_url(tenant, "/x").startswith("https://")
            )


class RootUrlconfStaysEmptyTests(SimpleTestCase):
    def test_root_urlconf_has_no_patterns(self):
        # The safety net must stay empty: dumping tenant patterns here would
        # let the console host resolve tenant-admin names and break host
        # isolation.
        import osds.urls

        self.assertEqual(osds.urls.urlpatterns, [])

    def test_reverse_of_a_tenant_route_fails_with_no_request(self):
        set_urlconf(None)  # clear any leak from an earlier test in this process
        self.addCleanup(set_urlconf, None)
        with self.assertRaises(NoReverseMatch):
            reverse("public-home")
        with self.assertRaises(NoReverseMatch):
            reverse("directory_admin:login")

    def test_reverse_works_only_with_the_urlconf_named_explicitly(self):
        set_urlconf(None)
        self.addCleanup(set_urlconf, None)
        self.assertEqual(
            reverse("public-home", urlconf=urlconf.TENANT_URLCONF), "/"
        )


class UrlconfConstantsTests(SimpleTestCase):
    def test_constants_name_the_three_importable_urlconf_modules(self):
        self.assertEqual(urlconf.TENANT_URLCONF, "osds.urls_tenant")
        self.assertEqual(urlconf.CONSOLE_URLCONF, "osds.urls_console")
        self.assertEqual(urlconf.SETUP_URLCONF, "osds.urls_setup")
        for name in (
            urlconf.TENANT_URLCONF,
            urlconf.CONSOLE_URLCONF,
            urlconf.SETUP_URLCONF,
        ):
            import_module(name)  # raises if the module is gone

    def test_middleware_assigns_request_urlconf_from_the_constants(self):
        src = inspect.getsource(
            middleware.TenantResolutionMiddleware.__call__
        )
        self.assertIn("TENANT_URLCONF", src)
        self.assertIn("CONSOLE_URLCONF", src)
        self.assertIn("SETUP_URLCONF", src)
        # no hardcoded module strings left behind
        self.assertNotIn('"osds.urls_tenant"', src)
        self.assertNotIn('"osds.urls_console"', src)
        self.assertNotIn('"osds.urls_setup"', src)
