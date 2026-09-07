"""Operator login / logout on a tenant host, the /admin/ landing, and the
anonymous redirect that issue #127 exists to fix.
"""

from __future__ import annotations

import functools
import re

from django.template.loader import render_to_string
from django.test import Client, TestCase, override_settings
from django.urls import reverse as _reverse
from django.utils import timezone

from tenants.models import InstallSetup, Operator, StaffMembership, Tenant

reverse = functools.partial(_reverse, urlconf="osds.urls_tenant")
HOST = "acme.test"
_FAST_HASH = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def _strip_csrf(html: bytes) -> bytes:
    return re.sub(
        rb'name="csrfmiddlewaretoken" value="[^"]*"',
        b'name="csrfmiddlewaretoken" value="X"',
        html,
    )


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class TenantLoginTests(TestCase):
    def setUp(self):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain=HOST
        )
        self.admin = Operator.objects.create_user(
            email="admin@acme.test", password="pw"
        )
        StaffMembership.objects.create(
            operator=self.admin,
            tenant=self.tenant,
            role=StaffMembership.Role.ADMIN,
            status=StaffMembership.Status.ACTIVE,
        )

    def _client(self, operator=None):
        c = Client()
        if operator:
            c.force_login(operator)
        return c

    # --- the #127 bug ----------------------------------------------------
    def test_anonymous_admin_request_redirects_to_login_not_404(self):
        resp = self._client().get(
            reverse("directory_admin:type-list"), HTTP_HOST=HOST
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp["Location"], "/admin/login/?next=/admin/listing-types/"
        )

    def test_admin_root_is_reachable_and_bounces_anonymous_to_login(self):
        resp = self._client().get("/admin/", HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/admin/login/?next=/admin/")

    def test_slashless_admin_redirect_preserves_the_query_string(self):
        resp = self._client().get(
            "/admin?next=/admin/listing-types/", HTTP_HOST=HOST
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp["Location"], "/admin/?next=/admin/listing-types/"
        )

    def test_wizard_completion_link_reaches_the_login_form(self):
        # setup/complete.html tells the operator to sign in at <domain>/admin
        html = render_to_string("setup/complete.html", {"tenant": self.tenant})
        self.assertIn("/admin", html)

        resp = self._client().get("/admin", HTTP_HOST=HOST, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sign in")
        self.assertIn("/admin/login/", resp.redirect_chain[-1][0])

    # --- login form ----------------------------------------------------
    def test_login_page_renders(self):
        resp = self._client().get(reverse("directory_admin:login"), HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sign in")
        self.assertContains(resp, 'name="password"')

    def test_valid_credentials_sign_in_and_follow_next(self):
        c = Client()
        resp = c.post(
            reverse("directory_admin:login"),
            {
                "username": "admin@acme.test",
                "password": "pw",
                "next": "/admin/listing-types/",
            },
            HTTP_HOST=HOST,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/admin/listing-types/")
        self.assertIn("_auth_user_id", c.session)

    def test_login_is_case_insensitive_on_the_email(self):
        c = Client()
        resp = c.post(
            reverse("directory_admin:login"),
            {"username": "Admin@Acme.TEST", "password": "pw"},
            HTTP_HOST=HOST,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", c.session)

    def test_bad_password_rerenders_without_a_session(self):
        c = Client()
        resp = c.post(
            reverse("directory_admin:login"),
            {"username": "admin@acme.test", "password": "wrong"},
            HTTP_HOST=HOST,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", c.session)

    def test_open_redirect_via_next_is_ignored(self):
        c = Client()
        resp = c.post(
            reverse("directory_admin:login"),
            {
                "username": "admin@acme.test",
                "password": "pw",
                "next": "https://evil.test/steal",
            },
            HTTP_HOST=HOST,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/admin/")

    # --- logout --------------------------------------------------------
    def test_logout_is_post_only_and_clears_the_session(self):
        c = self._client(self.admin)
        self.assertEqual(
            c.get(reverse("directory_admin:logout"), HTTP_HOST=HOST).status_code,
            405,
        )
        resp = c.post(reverse("directory_admin:logout"), HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/admin/login/")
        self.assertNotIn("_auth_user_id", c.session)


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class TenantLandingTests(TestCase):
    def setUp(self):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain=HOST
        )

    def _client(self, operator=None):
        c = Client()
        if operator:
            c.force_login(operator)
        return c

    def test_member_sees_the_dashboard_link(self):
        op = Operator.objects.create_user(email="a@acme.test", password="pw")
        StaffMembership.objects.create(
            operator=op,
            tenant=self.tenant,
            role=StaffMembership.Role.ADMIN,
            status=StaffMembership.Status.ACTIVE,
        )
        resp = self._client(op).get("/admin/", HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Listing types")

    def test_non_member_sees_a_landing_not_a_404(self):
        stranger = Operator.objects.create_user(email="s@x.test", password="pw")
        resp = self._client(stranger).get("/admin/", HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "no access")

    def test_landing_is_identical_regardless_of_other_tenant_memberships(self):
        other = Tenant.objects.create(
            slug="beta", name="Beta Co", primary_domain="beta.test"
        )
        op_elsewhere = Operator.objects.create_user(
            email="elsewhere@x.test", password="pw"
        )
        StaffMembership.objects.create(
            operator=op_elsewhere,
            tenant=other,
            role=StaffMembership.Role.ADMIN,
            status=StaffMembership.Status.ACTIVE,
        )
        op_nowhere = Operator.objects.create_user(email="nowhere@x.test", password="pw")

        a = self._client(op_elsewhere).get("/admin/", HTTP_HOST=HOST)
        b = self._client(op_nowhere).get("/admin/", HTTP_HOST=HOST)
        self.assertEqual(a.status_code, 200)
        self.assertEqual(b.status_code, 200)
        self.assertEqual(_strip_csrf(a.content), _strip_csrf(b.content))
        self.assertNotIn(b"Beta Co", a.content)

    def test_pending_membership_on_this_tenant_may_be_named(self):
        op = Operator.objects.create_user(email="p@x.test", password="pw")
        StaffMembership.objects.create(
            operator=op,
            tenant=self.tenant,
            role=StaffMembership.Role.EDITOR,
            status=StaffMembership.Status.PENDING,
        )
        resp = self._client(op).get("/admin/", HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "pending invitation")


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class LoginCsrfBehindTlsTerminationTests(TestCase):
    """SECURE_PROXY_SSL_HEADER: without it the CSRF origin check compares the
    browser's https Origin against an http request and 403s every login POST.
    """

    def setUp(self):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain=HOST
        )
        op = Operator.objects.create_user(email="admin@acme.test", password="pw")
        StaffMembership.objects.create(
            operator=op,
            tenant=self.tenant,
            role=StaffMembership.Role.ADMIN,
            status=StaffMembership.Status.ACTIVE,
        )

    def _token(self, client) -> str:
        resp = client.get(reverse("directory_admin:login"), HTTP_HOST=HOST)
        return resp.context["csrf_token"]

    def _post(self, client, token, **extra):
        return client.post(
            reverse("directory_admin:login"),
            {
                "username": "admin@acme.test",
                "password": "pw",
                "csrfmiddlewaretoken": token,
                "next": "/admin/",
            },
            HTTP_HOST=HOST,
            HTTP_ORIGIN=f"https://{HOST}",
            **extra,
        )

    def test_https_origin_without_forwarded_proto_is_a_403(self):
        c = Client(enforce_csrf_checks=True)
        resp = self._post(c, self._token(c))
        self.assertEqual(resp.status_code, 403)

    def test_forwarded_proto_https_lets_the_login_post_through(self):
        c = Client(enforce_csrf_checks=True)
        resp = self._post(c, self._token(c), HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", c.session)
