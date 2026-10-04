"""#234, decisions.md §4.13: ``X-Forwarded-Proto`` is believed only from a
trusted proxy.

The unit tests drive the middleware with RequestFactory. The raw-request class
at the bottom sends real bytes over a socket to a live server from a
non-loopback address, because the Django test Client never sets a real peer or
``wsgi.url_scheme`` (decisions.md §5).
"""

from __future__ import annotations

import http.client
import re
import socket
from unittest import mock

from django.conf import settings
from django.test import (
    LiveServerTestCase,
    RequestFactory,
    SimpleTestCase,
    override_settings,
)
from django.utils import timezone

from osds import client_ip as client_ip_mod
from osds.middleware import TrustedProxyHeadersMiddleware
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant

PROXY = "10.0.0.5"
TRUSTED = ("10.0.0.0/8",)


def scheme_seen(remote, proto=None, **extra):
    """The scheme the view behind the middleware sees."""
    meta = {"REMOTE_ADDR": remote, **extra}
    if proto is not None:
        meta["HTTP_X_FORWARDED_PROTO"] = proto
    seen = {}

    def view(request):
        seen["scheme"] = request.scheme
        seen["secure"] = request.is_secure()
        seen["header"] = request.META.get("HTTP_X_FORWARDED_PROTO")
        return mock.Mock()

    TrustedProxyHeadersMiddleware(view)(RequestFactory().get("/", **meta))
    return seen


class SettingsTests(SimpleTestCase):
    def test_it_is_the_first_middleware(self):
        self.assertEqual(
            settings.MIDDLEWARE[0], "osds.middleware.TrustedProxyHeadersMiddleware"
        )

    def test_the_other_forwarded_headers_stay_unread(self):
        self.assertFalse(settings.USE_X_FORWARDED_HOST)
        self.assertFalse(settings.USE_X_FORWARDED_PORT)
        self.assertEqual(
            settings.SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https")
        )


class UntrustedPeerTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(client_ip_mod, "_warned_proto", False))

    def test_no_trusted_proxies_means_the_header_is_deleted(self):
        seen = scheme_seen("8.8.8.8", "https")
        self.assertEqual(seen["scheme"], "http")
        self.assertFalse(seen["secure"])
        self.assertIsNone(seen["header"])

    @override_settings(OSDS_TRUSTED_PROXIES=TRUSTED)
    def test_a_peer_outside_the_list_is_not_believed(self):
        self.assertFalse(scheme_seen("8.8.8.8", "https")["secure"])

    @override_settings(OSDS_TRUSTED_PROXIES=TRUSTED)
    def test_an_ipv4_mapped_untrusted_peer_is_not_believed(self):
        self.assertFalse(scheme_seen("::ffff:8.8.8.8", "https")["secure"])

    @override_settings(OSDS_TRUSTED_PROXIES=TRUSTED)
    def test_an_empty_remote_addr_is_not_believed(self):
        self.assertFalse(scheme_seen("", "https")["secure"])

    def test_direct_tls_without_the_header_stays_secure(self):
        seen = scheme_seen("8.8.8.8", None, **{"wsgi.url_scheme": "https"})
        self.assertTrue(seen["secure"])

    def test_direct_tls_is_not_downgraded_by_a_forged_http_header(self):
        seen = scheme_seen("8.8.8.8", "http", **{"wsgi.url_scheme": "https"})
        self.assertTrue(seen["secure"])


@override_settings(OSDS_TRUSTED_PROXIES=TRUSTED)
class TrustedPeerTests(SimpleTestCase):
    def test_the_header_is_believed(self):
        self.assertTrue(scheme_seen(PROXY, "https")["secure"])
        self.assertFalse(scheme_seen(PROXY, "http")["secure"])

    def test_no_header_is_plain_http(self):
        self.assertFalse(scheme_seen(PROXY)["secure"])

    def test_a_client_written_leftmost_https_does_not_win(self):
        # The proxy appended its own observation to what the client sent.
        self.assertFalse(scheme_seen(PROXY, "https, http")["secure"])

    def test_the_rightmost_token_is_the_proxys(self):
        seen = scheme_seen(PROXY, "http, https")
        self.assertTrue(seen["secure"])
        self.assertEqual(seen["header"], "https")


class WarningTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(client_ip_mod, "_warned_proto", False))

    def test_a_private_untrusted_peer_claiming_https_warns_once(self):
        with self.assertLogs("osds.client_ip", "WARNING") as logs:
            scheme_seen("172.18.0.2", "https")
            scheme_seen("172.18.0.2", "https")
        self.assertEqual(len(logs.output), 1)
        self.assertIn("OSDS_TRUSTED_PROXIES", logs.output[0])

    def test_a_public_peer_does_not_warn(self):
        with self.assertNoLogs("osds.client_ip", "WARNING"):
            scheme_seen("8.8.8.8", "https")

    def test_loopback_and_http_do_not_warn(self):
        with self.assertNoLogs("osds.client_ip", "WARNING"):
            scheme_seen("127.0.0.1", "https")
            scheme_seen("172.18.0.2", "http")


# ---------------------------------------------------------------------------
# Raw requests
# ---------------------------------------------------------------------------

HOST = "acme.test"


def _lan_address() -> "str | None":
    """This machine's own non-loopback address, or ``None``. A UDP connect
    sends nothing; it only makes the OS choose a source address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            address = s.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


LAN = _lan_address()


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", DEBUG=False
)
class RawForwardedProtoTests(LiveServerTestCase):
    """Real sockets. The server binds every interface and the request goes to
    this machine's own non-loopback address, so ``REMOTE_ADDR`` is that address
    and not loopback. The observable is Django's CSRF origin check on the login
    POST, which compares the browser's https Origin with the request scheme."""

    host = "0.0.0.0"

    @classmethod
    def setUpClass(cls):
        if LAN is None:
            from unittest import SkipTest

            raise SkipTest("no non-loopback address on this machine")
        super().setUpClass()

    def setUp(self):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        tenant = Tenant.objects.create(slug="acme", name="Acme", primary_domain=HOST)
        op = Operator.objects.create_user(email="admin@acme.test", password="pw")
        StaffMembership.objects.create(
            operator=op,
            tenant=tenant,
            role=StaffMembership.Role.ADMIN,
            status=StaffMembership.Status.ACTIVE,
        )

    def _exchange(self, address, extra_headers):
        """GET the login page, then POST credentials, over a raw connection."""
        conn = http.client.HTTPConnection(address, self.server_thread.port, timeout=10)
        try:
            conn.request("GET", "/admin/login/", headers={"Host": HOST})
            page = conn.getresponse()
            body = page.read().decode()
            cookies = [v for k, v in page.getheaders() if k.lower() == "set-cookie"]
            cookie = "; ".join(c.split(";", 1)[0] for c in cookies)
            token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body).group(1)
            form = (
                f"username=admin%40acme.test&password=pw&next=%2Fadmin%2F"
                f"&csrfmiddlewaretoken={token}"
            )
            conn.request(
                "POST",
                "/admin/login/",
                body=form,
                headers={
                    "Host": HOST,
                    "Origin": f"https://{HOST}",
                    "Cookie": cookie,
                    "Content-Type": "application/x-www-form-urlencoded",
                    **extra_headers,
                },
            )
            return conn.getresponse().status
        finally:
            conn.close()

    def test_a_spoofed_header_from_an_untrusted_peer_is_ignored(self):
        with override_settings(OSDS_TRUSTED_PROXIES=("127.0.0.1/32",)):
            status = self._exchange(LAN, {"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 403)

    def test_a_spoofed_forwarded_for_does_not_make_the_peer_trusted(self):
        with override_settings(OSDS_TRUSTED_PROXIES=("127.0.0.1/32",)):
            status = self._exchange(
                LAN, {"X-Forwarded-Proto": "https", "X-Forwarded-For": "127.0.0.1"}
            )
        self.assertEqual(status, 403)

    def test_the_same_request_from_a_trusted_peer_is_believed(self):
        with override_settings(OSDS_TRUSTED_PROXIES=(f"{LAN}/32",)):
            status = self._exchange(LAN, {"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 302)
