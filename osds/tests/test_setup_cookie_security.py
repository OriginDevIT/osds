"""SetupCookieSecurityMiddleware: the first-run wizard is reached over plain
http before any TLS, so the Secure session/CSRF cookies (default on) must be
relaxed on the setup routes -- and only there, and only until setup is done.

The Django test Client ignores the Secure flag, so every assertion here reads
the Set-Cookie morsel attribute directly.
"""

from __future__ import annotations

import hashlib

from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from osds.middleware import SetupCookieSecurityMiddleware
from tenants.models import InstallSetup, Operator, Tenant


def _secure_cookie_response(request):
    resp = HttpResponse("ok")
    resp.set_cookie("sessionid", "s", secure=True)
    resp.set_cookie("csrftoken", "c", secure=True)
    return resp


class MiddlewareUnitTests(TestCase):
    def _run(self, path):
        mw = SetupCookieSecurityMiddleware(_secure_cookie_response)
        return mw(RequestFactory().get(path))

    def test_secure_stripped_on_a_setup_route_before_install(self):
        # No InstallSetup row at all -> setup is incomplete.
        resp = self._run("/setup/unlock/")
        self.assertFalse(resp.cookies["csrftoken"]["secure"])
        self.assertFalse(resp.cookies["sessionid"]["secure"])

    def test_secure_kept_outside_the_setup_routes(self):
        resp = self._run("/admin/login/")
        self.assertTrue(resp.cookies["csrftoken"]["secure"])
        self.assertTrue(resp.cookies["sessionid"]["secure"])

    def test_secure_kept_on_a_setup_route_once_setup_is_complete(self):
        # An operator exists and the wizard has finished.
        Operator.objects.create_user(email="a@b.test", password="x")
        InstallSetup.objects.create(
            token_hash="x" * 64, completed_at=timezone.now()
        )
        resp = self._run("/setup/unlock/")
        self.assertTrue(resp.cookies["csrftoken"]["secure"])
        self.assertTrue(resp.cookies["sessionid"]["secure"])

    def test_still_relaxed_mid_wizard_when_an_operator_exists_but_setup_is_not_done(self):
        # Account step is done (operator created) but domain/storage/etc. are
        # not: those POSTs are still plain http and still need relaxed cookies.
        Operator.objects.create_user(email="a@b.test", password="x")
        InstallSetup.objects.create(token_hash="x" * 64)  # completed_at is NULL
        resp = self._run("/setup/domain/")
        self.assertFalse(resp.cookies["csrftoken"]["secure"])
        self.assertFalse(resp.cookies["sessionid"]["secure"])

    def test_a_response_with_no_such_cookies_is_untouched(self):
        mw = SetupCookieSecurityMiddleware(lambda request: HttpResponse("ok"))
        resp = mw(RequestFactory().get("/setup/unlock/"))
        self.assertNotIn("csrftoken", resp.cookies)


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class MiddlewareThroughTheStackTests(TestCase):
    def test_wizard_unlock_response_carries_a_non_secure_csrf_cookie(self):
        InstallSetup.objects.create(
            token_hash=hashlib.sha256(b"tok").hexdigest()
        )
        resp = Client().get("/setup/unlock/", HTTP_HOST="console.test")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("csrftoken", resp.cookies)
        self.assertFalse(resp.cookies["csrftoken"]["secure"])

    def test_login_response_keeps_its_secure_csrf_cookie(self):
        InstallSetup.objects.create(
            token_hash="x" * 64, completed_at=timezone.now()
        )
        Tenant.objects.create(slug="acme", name="Acme", primary_domain="acme.test")
        resp = Client().get("/admin/login/", HTTP_HOST="acme.test")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("csrftoken", resp.cookies)
        self.assertTrue(resp.cookies["csrftoken"]["secure"])
