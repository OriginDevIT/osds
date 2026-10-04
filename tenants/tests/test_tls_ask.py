"""The on-demand TLS ask endpoint (decisions.md §4.13).

Allowed: a verified tenant domain (suspended included) and the console host.
Refused to everyone outside ``OSDS_TRUSTED_PROXIES`` by falling through to the
ordinary 404, and to a trusted peer that carries a forwarding header.
"""

from __future__ import annotations

import http.client
from unittest import SkipTest

from django.test import Client, LiveServerTestCase, TestCase, override_settings
from django.utils import timezone

from directory.services import RESERVED_SLUGS
from osds.tests.test_forwarded_proto import LAN
from tenants.models import InstallSetup, Operator, Tenant
from tenants.services import set_tenant_domain

CONSOLE = "console.example.test"
INTERNAL_HOST = "osds-app:8000"
PROXY = "10.0.0.5"
PATH = "/_internal/tls-ask"
TRUSTED = ("10.0.0.0/8",)


def _setup_complete():
    InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())


def _ask(domain, remote=PROXY, host=INTERNAL_HOST, path=PATH, **extra):
    return Client().get(
        path, {"domain": domain}, HTTP_HOST=host, REMOTE_ADDR=remote, **extra
    )


@override_settings(
    ALLOWED_HOSTS=["*"],
    OSDS_CONSOLE_HOST=CONSOLE,
    OSDS_TRUSTED_PROXIES=TRUSTED,
    DEBUG=False,
    OSDS_DEV_TENANT_SLUG="",
)
class AskTests(TestCase):
    def setUp(self):
        _setup_complete()
        self.tenant = Tenant.objects.create(
            slug="acme",
            name="Acme",
            primary_domain="acme.example.test",
            domain_verified_at=timezone.now(),
        )
        Tenant.objects.create(
            slug="fresh", name="Fresh", primary_domain="fresh.example.test"
        )

    def test_a_verified_tenant_domain_is_allowed(self):
        resp = _ask("acme.example.test")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Cache-Control"], "no-store")

    def test_case_whitespace_and_a_trailing_dot_are_normalised(self):
        self.assertEqual(_ask(" ACME.Example.Test. ").status_code, 200)

    def test_an_unverified_domain_is_refused(self):
        self.assertEqual(_ask("fresh.example.test").status_code, 404)

    def test_an_unknown_domain_is_refused(self):
        self.assertEqual(_ask("nobody.example.test").status_code, 404)

    def test_a_suspended_tenant_is_still_allowed(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(status=Tenant.Status.SUSPENDED)
        self.assertEqual(_ask("acme.example.test").status_code, 200)

    def test_the_console_host_is_allowed(self):
        self.assertEqual(_ask(CONSOLE).status_code, 200)

    @override_settings(OSDS_CONSOLE_HOST="")
    def test_an_empty_console_host_allows_nothing(self):
        self.assertEqual(_ask("").status_code, 404)

    def test_bad_input_is_refused(self):
        for bad in ("", "10.0.0.1", "acme.example.test/x", "acme.example.test:443", "a b"):
            with self.subTest(domain=bad):
                self.assertEqual(_ask(bad).status_code, 404)

    def test_a_missing_domain_parameter_is_refused(self):
        resp = Client().get(PATH, HTTP_HOST=INTERNAL_HOST, REMOTE_ADDR=PROXY)
        self.assertEqual(resp.status_code, 404)

    def test_a_changed_domain_is_not_allowed_until_verified_again(self):
        op = Operator.objects.create_user(email="o@x.test", password="pw")
        set_tenant_domain(tenant=self.tenant, domain="moved.example.test", changed_by=op)
        self.assertEqual(_ask("moved.example.test").status_code, 404)
        self.assertEqual(_ask("acme.example.test").status_code, 404)

    def test_it_answers_before_setup_is_complete(self):
        InstallSetup.objects.all().delete()
        InstallSetup.objects.create(token_hash="x" * 64)
        self.assertEqual(_ask(CONSOLE).status_code, 200)

    def test_head_is_answered_and_post_is_not(self):
        c = Client()
        head = c.head(
            PATH + "?domain=acme.example.test", HTTP_HOST=INTERNAL_HOST, REMOTE_ADDR=PROXY
        )
        self.assertEqual(head.status_code, 200)
        post = c.post(PATH, {"domain": "acme.example.test"}, HTTP_HOST=INTERNAL_HOST, REMOTE_ADDR=PROXY)
        self.assertEqual(post.status_code, 404)

    def test_a_malformed_host_does_not_matter(self):
        # The Host is never read: a header that get_host() would reject is fine.
        self.assertEqual(_ask("acme.example.test", host="bad host!").status_code, 200)

    def test_the_slug_is_reserved(self):
        self.assertIn("_internal", RESERVED_SLUGS)


@override_settings(
    ALLOWED_HOSTS=["*"],
    OSDS_CONSOLE_HOST=CONSOLE,
    OSDS_TRUSTED_PROXIES=TRUSTED,
    DEBUG=False,
    OSDS_DEV_TENANT_SLUG="",
)
class RefusalTests(TestCase):
    """Anyone else gets exactly what an unknown path gets."""

    def setUp(self):
        _setup_complete()
        Tenant.objects.create(
            slug="acme",
            name="Acme",
            primary_domain="acme.example.test",
            domain_verified_at=timezone.now(),
        )

    def _same_as_a_random_path(self, host, **extra):
        asked = _ask("acme.example.test", host=host, **extra)
        other = _ask("acme.example.test", host=host, path="/_internal/nothing-here", **extra)
        self.assertEqual(asked.status_code, other.status_code)
        self.assertEqual(asked.content, other.content)
        self.assertNotEqual(asked.status_code, 200)

    def test_an_untrusted_peer_gets_the_ordinary_404_on_an_unknown_host(self):
        self._same_as_a_random_path(INTERNAL_HOST, remote="8.8.8.8")

    def test_an_untrusted_peer_gets_the_ordinary_404_on_a_tenant_host(self):
        self._same_as_a_random_path("acme.example.test", remote="8.8.8.8")

    def test_a_spoofed_forwarded_for_does_not_make_a_peer_trusted(self):
        self._same_as_a_random_path(
            INTERNAL_HOST, remote="8.8.8.8", HTTP_X_FORWARDED_FOR=PROXY
        )

    @override_settings(OSDS_TRUSTED_PROXIES=())
    def test_with_no_trusted_proxies_the_endpoint_is_dark(self):
        self._same_as_a_random_path(INTERNAL_HOST, remote="127.0.0.1")

    def test_a_trusted_peer_relaying_traffic_is_refused(self):
        for header in (
            "HTTP_X_FORWARDED_FOR",
            "HTTP_X_FORWARDED_HOST",
            "HTTP_X_FORWARDED_PROTO",
            "HTTP_X_FORWARDED_PORT",
            "HTTP_FORWARDED",
            "HTTP_X_REAL_IP",
        ):
            with self.subTest(header=header):
                resp = _ask("acme.example.test", **{header: "1.2.3.4"})
                self.assertEqual(resp.status_code, 404)

    def test_the_console_host_is_not_swallowed(self):
        resp = Client().get("/", HTTP_HOST=CONSOLE, REMOTE_ADDR=PROXY)
        self.assertNotEqual(resp.content, b"ok\n")


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST=CONSOLE, DEBUG=False, OSDS_DEV_TENANT_SLUG=""
)
class RawAskTests(LiveServerTestCase):
    """Real sockets from this machine's own non-loopback address."""

    host = "0.0.0.0"

    @classmethod
    def setUpClass(cls):
        if LAN is None:
            raise SkipTest("no non-loopback address on this machine")
        super().setUpClass()

    def setUp(self):
        _setup_complete()
        Tenant.objects.create(
            slug="acme",
            name="Acme",
            primary_domain="acme.example.test",
            domain_verified_at=timezone.now(),
        )

    def _get(self, headers=None):
        conn = http.client.HTTPConnection(LAN, self.server_thread.port, timeout=10)
        try:
            conn.request(
                "GET",
                PATH + "?domain=acme.example.test",
                headers={"Host": INTERNAL_HOST, **(headers or {})},
            )
            return conn.getresponse().status
        finally:
            conn.close()

    def test_an_untrusted_peer_is_refused(self):
        with override_settings(OSDS_TRUSTED_PROXIES=("127.0.0.1/32",)):
            self.assertEqual(self._get(), 404)

    def test_a_spoofed_forwarded_for_does_not_help(self):
        with override_settings(OSDS_TRUSTED_PROXIES=("127.0.0.1/32",)):
            self.assertEqual(self._get({"X-Forwarded-For": "127.0.0.1"}), 404)

    def test_a_trusted_peer_is_answered(self):
        with override_settings(OSDS_TRUSTED_PROXIES=(f"{LAN}/32",)):
            self.assertEqual(self._get(), 200)

    def test_a_trusted_peer_relaying_a_visitor_is_refused(self):
        with override_settings(OSDS_TRUSTED_PROXIES=(f"{LAN}/32",)):
            self.assertEqual(self._get({"X-Forwarded-For": "203.0.113.9"}), 404)
