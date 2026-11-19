"""#139: an operator login over plain http with Secure cookies enabled.

A browser drops a ``Secure`` cookie set from an http origin, so the session
never sticks and a correct login bounces back to the form. The login pages
must say so instead of looping silently.
"""

from __future__ import annotations

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from tenants.models import InstallSetup, Operator, Tenant

CONSOLE = "console.test"
TENANT = "acme.test"
_FAST_HASH = ["django.contrib.auth.hashers.MD5PasswordHasher"]
# The diagnostic names the env var so the operator knows what to change.
MARKER = "OSDS_SECURE_COOKIES"

PAGES = (("console", "/login/", CONSOLE), ("tenant admin", "/admin/login/", TENANT))


@override_settings(
    ALLOWED_HOSTS=["*"],
    OSDS_CONSOLE_HOST=CONSOLE,
    PASSWORD_HASHERS=_FAST_HASH,
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
)
class HttpSecureCookieDiagnosticTests(TestCase):
    def setUp(self):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        Tenant.objects.create(slug="acme", name="Acme", primary_domain=TENANT)
        Operator.objects.create_user(email="op@example.test", password="pw")

    def test_login_page_over_http_with_secure_cookies_says_so(self):
        for name, path, host in PAGES:
            with self.subTest(page=name):
                resp = Client().get(path, HTTP_HOST=host)
                self.assertContains(resp, MARKER)

    def test_login_page_over_https_has_no_diagnostic(self):
        for name, path, host in PAGES:
            with self.subTest(page=name):
                resp = Client().get(path, HTTP_HOST=host, secure=True)
                self.assertNotContains(resp, MARKER)

    def test_login_page_over_http_with_cookies_not_secure_has_no_diagnostic(self):
        for name, path, host in PAGES:
            with self.subTest(page=name), self.settings(
                SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False
            ):
                resp = Client().get(path, HTTP_HOST=host)
                self.assertNotContains(resp, MARKER)

    def test_failed_login_over_http_with_secure_cookies_says_so(self):
        for name, path, host in PAGES:
            with self.subTest(page=name):
                resp = Client().post(
                    path,
                    {"username": "op@example.test", "password": "nope"},
                    HTTP_HOST=host,
                )
                self.assertContains(resp, MARKER)

    def test_correct_login_that_bounces_back_lands_on_the_diagnostic(self):
        """The reported loop: right credentials, redirect, form again. The
        browser drops the Secure session cookie, modelled by clearing the
        client's jar before it follows the redirect."""
        c = Client()
        resp = c.post(
            "/login/",
            {"username": "op@example.test", "password": "pw"},
            HTTP_HOST=CONSOLE,
        )
        self.assertEqual(resp.status_code, 302)
        c.cookies.clear()
        landing = c.get(resp["Location"], HTTP_HOST=CONSOLE, follow=True)
        self.assertContains(landing, MARKER)
