"""osds.adapter_context: scope grants, the context builder, and egress for an
operator-supplied destination (decisions.md §4.12).

The egress tests pin down what the policy refuses and, as important, what it
lets through; the address checks are the part that keeps a webhook URL from being
a way to reach the cloud metadata service or the server's own loopback.
"""

from __future__ import annotations

import http.server
import threading
import time
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from osds import adapter_context as ac
from osds.adapter_api import EgressDenied, HttpResponse, SecretNotFound
from tenants.models import Tenant
from tenants.secrets import set_secret

PUBLIC = "93.184.216.34"
HOST = "hooks.example.test"


class ClassifyTests(SimpleTestCase):
    def test_public(self):
        for addr in ("8.8.8.8", PUBLIC, "2606:4700:4700::1111", "::ffff:8.8.8.8"):
            self.assertEqual(ac.classify_address(addr), "public", addr)

    def test_private(self):
        for addr in ("10.1.2.3", "172.16.0.5", "192.168.1.1", "100.64.0.1", "fd00::1", "203.0.113.9", "::ffff:10.0.0.1"):
            self.assertEqual(ac.classify_address(addr), "private", addr)

    def test_never_reachable(self):
        for addr in (
            "127.0.0.1", "127.5.5.5", "::1", "169.254.169.254", "169.254.1.1", "fe80::1",
            "0.0.0.0", "224.0.0.1", "255.255.255.255", "fd00:ec2::254",
            "::ffff:169.254.169.254", "::ffff:127.0.0.1",
        ):
            self.assertEqual(ac.classify_address(addr), "denied", addr)


class CheckDestinationTests(SimpleTestCase):
    def check(self, scheme, addresses, allow_private=False):
        ac.check_destination(scheme, addresses, allow_private=allow_private)

    def test_public_https_is_fine(self):
        self.check("https", [PUBLIC])

    def test_private_needs_the_opt_in(self):
        with self.assertRaises(EgressDenied):
            self.check("https", ["10.0.0.5"])
        self.check("https", ["10.0.0.5"], allow_private=True)

    def test_one_bad_answer_spoils_a_name_that_also_resolves_public(self):
        with self.assertRaises(EgressDenied):
            self.check("https", [PUBLIC, "10.0.0.5"])
        with self.assertRaises(EgressDenied):
            self.check("https", [PUBLIC, "169.254.169.254"], allow_private=True)

    def test_metadata_and_loopback_are_refused_even_with_the_opt_in(self):
        for addr in ("169.254.169.254", "127.0.0.1", "::1", "fd00:ec2::254"):
            with self.subTest(addr=addr):
                with self.assertRaises(EgressDenied):
                    self.check("https", [addr], allow_private=True)
                with self.assertRaises(EgressDenied):
                    self.check("http", [addr], allow_private=True)

    def test_plain_http_only_to_private_addresses_and_only_with_the_opt_in(self):
        with self.assertRaises(EgressDenied):
            self.check("http", [PUBLIC], allow_private=True)
        with self.assertRaises(EgressDenied):
            self.check("http", ["10.0.0.5"])
        with self.assertRaises(EgressDenied):
            self.check("http", [PUBLIC, "10.0.0.5"], allow_private=True)
        self.check("http", ["10.0.0.5"], allow_private=True)

    def test_no_answer_is_a_refusal(self):
        with self.assertRaises(EgressDenied):
            self.check("https", [])


class _Transport:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or HttpResponse(200, b"ok")

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _fetch(hosts=(HOST,), *, resolves=(PUBLIC,), allow_private=False):
    resolver = mock.Mock(return_value=list(resolves))
    transport = _Transport()
    fetch = ac.make_operator_http(
        hosts, allow_private=allow_private, resolver=resolver, transport=transport
    )
    return fetch, resolver, transport


class OperatorHttpTests(SimpleTestCase):
    def test_a_good_request_is_pinned_to_the_address_that_was_checked(self):
        fetch, resolver, transport = _fetch(resolves=(PUBLIC, "93.184.216.35"))
        fetch("POST", f"https://{HOST}/in?token=abc", headers={"X-A": "1"}, body=b"x")
        [call] = transport.calls
        self.assertEqual(
            (call["scheme"], call["host"], call["port"], call["ip"], call["target"], call["method"]),
            ("https", HOST, 443, PUBLIC, "/in?token=abc", "POST"),
        )
        self.assertEqual(call["headers"], {"X-A": "1"})
        resolver.assert_called_once_with(HOST)  # resolved once: no second lookup to rebind

    def test_the_host_must_be_the_configured_one(self):
        fetch, _, transport = _fetch()
        for url in ("https://evil.example.test/", "https://hooks.example.test.evil.test/", "https://sub.hooks.example.test/"):
            with self.subTest(url=url):
                with self.assertRaises(EgressDenied):
                    fetch("POST", url)
        self.assertEqual(transport.calls, [])

    def test_host_matching_ignores_case(self):
        fetch, _, transport = _fetch()
        fetch("POST", "https://HOOKS.Example.TEST/x")
        self.assertEqual(len(transport.calls), 1)

    def test_nothing_is_allowed_when_no_host_is_configured(self):
        fetch, _, _ = _fetch(hosts=())
        with self.assertRaises(EgressDenied):
            fetch("POST", f"https://{HOST}/")

    def test_credentials_in_the_url_other_schemes_and_bad_ports_are_refused(self):
        fetch, _, _ = _fetch()
        for url in (f"https://user:pw@{HOST}/", f"https://user@{HOST}/", f"ftp://{HOST}/", f"https://{HOST}:99999/", "https:///x"):
            with self.subTest(url=url):
                with self.assertRaises(EgressDenied):
                    fetch("POST", url)

    def test_a_name_that_resolves_private_is_refused_unless_opted_in(self):
        fetch, _, _ = _fetch(resolves=("10.0.0.5",))
        with self.assertRaises(EgressDenied):
            fetch("POST", f"https://{HOST}/")
        fetch, _, transport = _fetch(resolves=("10.0.0.5",), allow_private=True)
        fetch("POST", f"https://{HOST}/")
        self.assertEqual(transport.calls[0]["ip"], "10.0.0.5")

    def test_the_metadata_service_is_unreachable_by_name_or_literal_even_when_opted_in(self):
        fetch, _, transport = _fetch(resolves=("169.254.169.254",), allow_private=True)
        with self.assertRaises(EgressDenied):
            fetch("POST", f"https://{HOST}/latest/meta-data/")
        fetch, resolver, transport = _fetch(hosts=("169.254.169.254",), allow_private=True)
        with self.assertRaises(EgressDenied):
            fetch("POST", "http://169.254.169.254/latest/meta-data/")
        resolver.assert_not_called()
        self.assertEqual(transport.calls, [])

    def test_loopback_is_unreachable_even_when_opted_in(self):
        fetch, _, _ = _fetch(hosts=("127.0.0.1", "localhost"), resolves=("127.0.0.1",), allow_private=True)
        for url in ("http://127.0.0.1:8000/", "https://localhost/"):
            with self.subTest(url=url):
                with self.assertRaises(EgressDenied):
                    fetch("POST", url)

    def test_an_ip_literal_is_checked_without_a_lookup(self):
        fetch, resolver, transport = _fetch(hosts=("2606:4700:4700::1111",))
        fetch("POST", "https://[2606:4700:4700::1111]/x")
        resolver.assert_not_called()
        self.assertEqual(transport.calls[0]["ip"], "2606:4700:4700::1111")
        fetch, _, _ = _fetch(hosts=("10.0.0.5",))
        with self.assertRaises(EgressDenied):
            fetch("POST", "https://10.0.0.5/")

    def test_plain_http_to_a_private_address_needs_the_opt_in(self):
        fetch, _, _ = _fetch(resolves=("10.0.0.5",))
        with self.assertRaises(EgressDenied):
            fetch("POST", f"http://{HOST}/")
        fetch, _, transport = _fetch(resolves=("10.0.0.5",), allow_private=True)
        fetch("POST", f"http://{HOST}:8080/hook")
        call = transport.calls[0]
        self.assertEqual((call["scheme"], call["port"]), ("http", 8080))
        fetch, _, _ = _fetch(resolves=(PUBLIC,), allow_private=True)
        with self.assertRaises(EgressDenied):
            fetch("POST", f"http://{HOST}/")  # plain http never to a public address

    def test_the_deadline_and_connect_timeout_are_bounded(self):
        fetch, _, transport = _fetch()
        before = time.monotonic()
        fetch("POST", f"https://{HOST}/", timeout=300)
        call = transport.calls[0]
        self.assertLessEqual(call["timeout"], ac.CONNECT_TIMEOUT)
        self.assertLessEqual(call["deadline"] - before, ac.TOTAL_DEADLINE + 1)

    def test_a_response_comes_back_untouched(self):
        fetch, _, transport = _fetch()
        transport.response = HttpResponse(429, b"slow down", {"retry-after": "30"})
        response = fetch("POST", f"https://{HOST}/")
        self.assertEqual((response.status, response.headers["retry-after"]), (429, "30"))

    def test_a_name_that_does_not_resolve_is_a_refusal(self):
        resolver = mock.Mock(side_effect=EgressDenied("the host did not resolve"))
        fetch = ac.make_operator_http((HOST,), resolver=resolver, transport=_Transport())
        with self.assertRaises(EgressDenied):
            fetch("POST", f"https://{HOST}/")


class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Recorder.seen.append((self.path, self.headers.get("Host"), body))
        payload = b"x" * 200_000 if self.path == "/big" else b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Echo", "yes")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class PinnedTransportTests(SimpleTestCase):
    """The real transport, against a local server. The address policy lives in
    ``check_destination``; this is about *how* the socket is opened."""

    def setUp(self):
        _Recorder.seen = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # Cleanups run last-in first-out: stop serving, then close the socket.
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, target="/x?y=1", *, deadline=None, ip="127.0.0.1"):
        return ac.pinned_transport(
            scheme="http", host="hooks.example.test", port=self.port, ip=ip, method="POST",
            target=target, headers={"X-A": "1"}, body=b"hello", timeout=5,
            deadline=deadline if deadline is not None else time.monotonic() + 5,
        )

    def test_it_connects_to_the_given_address_and_names_the_host(self):
        # "hooks.example.test" does not resolve anywhere: only the pin gets us here.
        response = self.call()
        self.assertEqual((response.status, response.body, response.headers["x-echo"]), (200, b"ok", "yes"))
        path, host, body = _Recorder.seen[0]
        self.assertEqual((path, body), ("/x?y=1", b"hello"))
        self.assertEqual(host, f"hooks.example.test:{self.port}")

    def test_the_response_is_capped(self):
        response = self.call("/big")
        self.assertEqual(len(response.body), ac.MAX_RESPONSE_BYTES)

    def test_a_spent_deadline_stops_the_read(self):
        with self.assertRaises(TimeoutError):
            self.call(deadline=time.monotonic() - 1)

    def test_a_refused_connection_raises_an_oserror(self):
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaises(OSError):
            self.call()

    def test_tls_verifies_the_certificate_against_the_hostname_not_the_address(self):
        context = mock.Mock()
        sock = object()
        with mock.patch("osds.adapter_context.socket.create_connection", return_value=sock) as connect:
            conn = ac._PinnedHTTPSConnection("hooks.example.test", 443, ip="93.184.216.34", timeout=7, context=context)
            conn.connect()
        connect.assert_called_once_with(("93.184.216.34", 443), 7)
        context.wrap_socket.assert_called_once_with(sock, server_hostname="hooks.example.test")


class GrantedScopesTests(SimpleTestCase):
    def sub(self, **kw):
        return SimpleNamespace(id="hook", **kw)

    def tenant(self, config):
        return SimpleNamespace(settings={"adapters": {"hook": config}})

    def test_only_what_is_both_requested_and_turned_on(self):
        sub = self.sub(scopes=frozenset({"pii:contact", "pii:message"}))
        t = self.tenant({"scope_pii_contact": True, "scope_pii_message": False})
        self.assertEqual(ac.granted_scopes(sub, t), {"pii:contact"})

    def test_a_scope_that_was_not_requested_is_never_granted(self):
        sub = self.sub(scopes=frozenset({"pii:message"}))
        self.assertEqual(ac.granted_scopes(sub, self.tenant({"scope_pii_contact": True})), frozenset())

    def test_a_subscriber_that_asks_for_nothing_gets_nothing(self):
        self.assertEqual(ac.granted_scopes(self.sub(), self.tenant({"scope_pii_contact": True})), frozenset())

    def test_nothing_without_a_tenant(self):
        sub = self.sub(scopes=frozenset({"pii:contact"}))
        self.assertEqual(ac.granted_scopes(sub, None), frozenset())

    def test_only_a_real_true_grants(self):
        sub = self.sub(scopes=frozenset({"pii:contact"}))
        for value in ("true", "yes", 1, "1", None, ""):
            with self.subTest(value=value):
                self.assertEqual(ac.granted_scopes(sub, self.tenant({"scope_pii_contact": value})), frozenset())

    def test_the_config_key_is_honoured(self):
        sub = self.sub(scopes=frozenset({"pii:contact"}), config_key="legacy")
        t = SimpleNamespace(settings={"legacy": {"scope_pii_contact": True}})
        self.assertEqual(ac.granted_scopes(sub, t), {"pii:contact"})

    def test_scope_setting_names(self):
        self.assertEqual(ac.scope_setting("pii:contact"), "scope_pii_contact")


class _Provider:
    adapter_id = "demo"
    egress_allowlist = ("api.example.test",)


class BuildContextTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain="acme.test",
            settings={"adapters": {"demo": {"flag": True}}, "legacy": {"x": 1}},
        )

    def test_the_context_carries_the_tenants_identity_and_the_adapters_config(self):
        ctx = ac.build_context(self.tenant, _Provider())
        self.assertEqual(
            (ctx.tenant_id, ctx.tenant_slug, ctx.tenant_domain, ctx.config),
            (self.tenant.public_id, "acme", "acme.test", {"flag": True}),
        )

    def test_config_is_a_copy(self):
        ctx = ac.build_context(self.tenant, _Provider())
        ctx.config["flag"] = False
        self.assertIs(self.tenant.settings["adapters"]["demo"]["flag"], True)

    def test_a_config_key_reads_another_place(self):
        provider = type("P", (), {"adapter_id": "demo", "config_key": "legacy"})()
        self.assertEqual(ac.build_context(self.tenant, provider).config, {"x": 1})

    def test_an_id_only_subscriber_works(self):
        ctx = ac.build_context(self.tenant, SimpleNamespace(id="demo"))
        self.assertEqual(ctx.config, {"flag": True})

    def test_secrets_resolve_and_a_missing_one_raises_secret_not_found(self):
        set_secret("demo_token", "tok-123", tenant=self.tenant)
        ctx = ac.build_context(self.tenant, _Provider())
        self.assertEqual(ctx.secret("token"), "tok-123")
        with self.assertRaises(SecretNotFound):
            ctx.secret("nope")

    def test_a_resolved_secret_is_scrubbed_from_what_the_adapter_logs(self):
        set_secret("demo_token", "tok-very-secret-123", tenant=self.tenant)
        ctx = ac.build_context(self.tenant, _Provider())
        ctx.secret("token")
        with self.assertLogs("osds.adapters.demo", level="INFO") as logs:
            ctx.logger.info("calling with tok-very-secret-123 now")
        self.assertEqual(logs.output, ["INFO:osds.adapters.demo:calling with [redacted] now"])

    def test_a_secret_not_yet_resolved_is_not_known_to_the_logger(self):
        set_secret("demo_token", "tok-never-asked-for", tenant=self.tenant)
        ctx = ac.build_context(self.tenant, _Provider())
        with self.assertLogs("osds.adapters.demo", level="INFO") as logs:
            ctx.logger.info("hello tok-never-asked-for")
        self.assertIn("tok-never-asked-for", logs.output[0])  # nothing resolved it: nothing to scrub

    def test_a_static_allowlist_is_exact_https_hostnames(self):
        ctx = ac.build_context(self.tenant, _Provider(), opener=mock.Mock())
        with self.assertRaises(EgressDenied):
            ctx.http("GET", "https://other.example.test/")
        with self.assertRaises(EgressDenied):
            ctx.http("GET", "http://api.example.test/")

    def test_an_operator_destination_uses_the_strict_policy(self):
        provider = type("P", (), {
            "adapter_id": "demo", "egress_allowlist_for": lambda self, config: ("hooks.example.test",),
        })()
        transport = _Transport()
        ctx = ac.build_context(
            self.tenant, provider, resolver=lambda host: ["10.0.0.9"], transport=transport
        )
        with self.assertRaises(EgressDenied):
            ctx.http("POST", f"https://{HOST}/")

    def test_the_deployment_opt_in_reaches_the_context(self):
        provider = type("P", (), {
            "adapter_id": "demo", "egress_allowlist_for": lambda self, config: ("hooks.example.test",),
        })()
        transport = _Transport()
        with override_settings(OSDS_WEBHOOK_ALLOW_PRIVATE=True):
            ctx = ac.build_context(
                self.tenant, provider, resolver=lambda host: ["10.0.0.9"], transport=transport
            )
        ctx.http("POST", f"https://{HOST}/")
        self.assertEqual(transport.calls[0]["ip"], "10.0.0.9")

    def test_the_setting_defaults_off(self):
        from django.conf import settings

        self.assertIs(settings.OSDS_WEBHOOK_ALLOW_PRIVATE, False)

    def test_the_clock_is_timezone_now(self):
        ctx = ac.build_context(self.tenant, _Provider())
        self.assertIsNotNone(ctx.clock().tzinfo)
