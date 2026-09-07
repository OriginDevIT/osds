"""The installation console: operator login / logout and the directory picker.

Every operator can sign in here, membership or not -- accepting a pending
invitation happens on the console (decisions.md section 3).
"""

from __future__ import annotations

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from tenants.models import InstallSetup, Operator, StaffMembership, Tenant

CONSOLE = "console.test"
_FAST_HASH = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST=CONSOLE, PASSWORD_HASHERS=_FAST_HASH
)
class ConsoleAuthTests(TestCase):
    def setUp(self):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.operator = Operator.objects.create_user(
            email="op@example.test", password="pw"
        )

    def _client(self, operator=None):
        c = Client()
        if operator:
            c.force_login(operator)
        return c

    def test_index_anonymous_redirects_to_the_console_login(self):
        resp = self._client().get("/", HTTP_HOST=CONSOLE)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/login/?next=/")

    def test_login_form_renders(self):
        resp = self._client().get("/login/", HTTP_HOST=CONSOLE)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sign in")

    def test_operator_without_any_membership_can_sign_in(self):
        c = Client()
        resp = c.post(
            "/login/",
            {"username": "op@example.test", "password": "pw"},
            HTTP_HOST=CONSOLE,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/")
        self.assertIn("_auth_user_id", c.session)
        page = c.get("/", HTTP_HOST=CONSOLE)
        self.assertContains(page, "no directories yet")

    def test_login_lowercases_the_email(self):
        c = Client()
        resp = c.post(
            "/login/",
            {"username": "OP@Example.TEST", "password": "pw"},
            HTTP_HOST=CONSOLE,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", c.session)

    def test_bad_password_does_not_start_a_session(self):
        c = Client()
        resp = c.post(
            "/login/",
            {"username": "op@example.test", "password": "nope"},
            HTTP_HOST=CONSOLE,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", c.session)

    def test_index_lists_active_memberships_linking_to_the_tenant_admin(self):
        t = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain="acme.test"
        )
        StaffMembership.objects.create(
            operator=self.operator,
            tenant=t,
            role=StaffMembership.Role.MANAGER,
            status=StaffMembership.Status.ACTIVE,
        )
        resp = self._client(self.operator).get("/", HTTP_HOST=CONSOLE)
        self.assertContains(resp, 'href="https://acme.test/admin/"')
        self.assertContains(resp, "Acme")

    def test_index_shows_pending_invitations(self):
        t = Tenant.objects.create(
            slug="beta", name="Beta", primary_domain="beta.test"
        )
        StaffMembership.objects.create(
            operator=self.operator,
            tenant=t,
            role=StaffMembership.Role.EDITOR,
            status=StaffMembership.Status.PENDING,
        )
        resp = self._client(self.operator).get("/", HTTP_HOST=CONSOLE)
        self.assertContains(resp, "Pending invitations")
        self.assertContains(resp, "Beta")

    def test_logout_is_post_only_and_clears_the_session(self):
        c = self._client(self.operator)
        self.assertEqual(c.get("/logout/", HTTP_HOST=CONSOLE).status_code, 405)
        resp = c.post("/logout/", HTTP_HOST=CONSOLE)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/login/")
        self.assertNotIn("_auth_user_id", c.session)
