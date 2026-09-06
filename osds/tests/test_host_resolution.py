"""Host resolution: console host, tenant host, unknown host, and that the
ambient tenant scope is always torn down.
"""

from __future__ import annotations

import importlib

from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from osds.middleware import TenantResolutionMiddleware
from osds.tenancy import get_current_tenant
from tenants.models import Tenant

CONSOLE = "console.example.test"


@override_settings(
    OSDS_CONSOLE_HOST=CONSOLE, ALLOWED_HOSTS=["*"], DEBUG=False, OSDS_DEV_TENANT_SLUG=""
)
class HostResolutionTests(TestCase):
    def setUp(self):
        self.rf = RequestFactory()
        self.seen: dict = {}

        def get_response(request):
            self.seen["reached_view"] = True
            self.seen["kind"] = request.osds_host_kind
            self.seen["tenant"] = request.tenant
            self.seen["scope"] = get_current_tenant()
            return HttpResponse("ok")

        self.mw = TenantResolutionMiddleware(get_response)

    def _call(self, host, path="/", get_response=None):
        mw = TenantResolutionMiddleware(get_response) if get_response else self.mw
        request = self.rf.get(path, HTTP_HOST=host)
        return mw(request), request

    # --- console -----------------------------------------------------------
    def test_console_host_routes_to_console_urlconf_with_no_scope(self):
        response, request = self._call(CONSOLE)
        self.assertTrue(self.seen["reached_view"])
        self.assertEqual(request.osds_host_kind, "console")
        self.assertIsNone(request.tenant)
        self.assertIsNone(self.seen["scope"])
        self.assertEqual(request.urlconf, "osds.urls_console")

    @override_settings(OSDS_CONSOLE_HOST="")
    def test_no_console_host_configured_means_nothing_is_the_console(self):
        response, request = self._call(CONSOLE)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(request.osds_host_kind, "unknown")

    # --- tenant ----------------------------------------------------------
    def test_tenant_host_sets_request_tenant_scope_and_urlconf(self):
        tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain="acme.example.test"
        )
        response, request = self._call("acme.example.test")
        self.assertEqual(request.osds_host_kind, "tenant")
        self.assertEqual(request.tenant, tenant)
        self.assertEqual(self.seen["scope"], tenant)
        self.assertEqual(request.urlconf, "osds.urls_tenant")

    def test_host_is_matched_case_insensitively_and_ignores_port(self):
        tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain="acme.example.test"
        )
        _, request = self._call("ACME.Example.Test:8443")
        self.assertEqual(request.tenant, tenant)

    def test_unverified_domain_still_routes(self):
        tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain="acme.example.test"
        )
        self.assertIsNone(tenant.domain_verified_at)
        _, request = self._call("acme.example.test")
        self.assertEqual(request.osds_host_kind, "tenant")

    def test_suspended_tenant_returns_503_and_no_scope(self):
        Tenant.objects.create(
            slug="susp",
            name="Susp",
            primary_domain="susp.example.test",
            status=Tenant.Status.SUSPENDED,
        )
        response, request = self._call("susp.example.test")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(request.osds_host_kind, "suspended")
        self.assertNotIn("reached_view", self.seen)
        self.assertIsNone(get_current_tenant())

    # --- unknown -------------------------------------------------------------
    def test_unknown_host_returns_404_without_reaching_a_view(self):
        response, request = self._call("stranger.example.test")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(request.osds_host_kind, "unknown")
        self.assertIsNone(request.tenant)
        self.assertNotIn("reached_view", self.seen)
        self.assertIsNone(get_current_tenant())

    def test_unknown_host_is_not_a_redirect_and_not_the_console(self):
        response, _ = self._call("stranger.example.test")
        self.assertNotIn(response.status_code, (301, 302, 307, 308))
        self.assertNotIn(b"admin", response.content)

    # --- dev fallback ------------------------------------------------------
    @override_settings(DEBUG=True, OSDS_DEV_TENANT_SLUG="chicago-plumbers")
    def test_dev_slug_fallback_only_when_debug(self):
        tenant = Tenant.objects.create(slug="chicago-plumbers", name="CP")
        _, request = self._call("localhost")
        self.assertEqual(request.osds_host_kind, "tenant")
        self.assertEqual(request.tenant, tenant)

    def test_dev_slug_ignored_when_not_debug(self):
        Tenant.objects.create(slug="chicago-plumbers", name="CP")
        with override_settings(DEBUG=False, OSDS_DEV_TENANT_SLUG="chicago-plumbers"):
            response, _ = self._call("localhost")
        self.assertEqual(response.status_code, 404)

    # --- scope teardown --------------------------------------------------
    def test_scope_is_cleared_after_a_normal_response(self):
        Tenant.objects.create(
            slug="acme", name="Acme", primary_domain="acme.example.test"
        )
        self._call("acme.example.test")
        self.assertIsNone(get_current_tenant())

    def test_scope_is_cleared_when_the_view_raises(self):
        Tenant.objects.create(
            slug="acme", name="Acme", primary_domain="acme.example.test"
        )

        def boom(request):
            raise ValueError("kaboom")

        with self.assertRaises(ValueError):
            self._call("acme.example.test", get_response=boom)
        self.assertIsNone(get_current_tenant())

    # --- wiring -----------------------------------------------------------
    def test_both_switched_urlconfs_import(self):
        importlib.import_module("osds.urls_console")
        importlib.import_module("osds.urls_tenant")
