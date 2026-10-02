"""osds.client_ip (#210, decisions.md §4.8): who sent this request.

Pure -- RequestFactory, no database. The point of most of these is what is
*not* believed: a client writes X-Forwarded-For, so it is read only from a
trusted proxy, and only from the right.
"""

from __future__ import annotations

from django.test import RequestFactory, SimpleTestCase, override_settings

from osds import client_ip as mod
from osds.client_ip import client_ip, ip_bucket, parse_networks

PROXY = "10.0.0.5"
TRUSTED = ("10.0.0.0/8", "fd00::/8")


def req(remote="8.8.8.8", xff=None, **extra):
    meta = {"REMOTE_ADDR": remote, **extra}
    if xff is not None:
        meta["HTTP_X_FORWARDED_FOR"] = xff
    return RequestFactory().get("/", **meta)


class _Base(SimpleTestCase):
    def setUp(self):
        self.enterContext(mod_warned(False))


def mod_warned(value):
    from unittest import mock

    return mock.patch.object(mod, "_warned", value)


class NoTrustedProxiesTests(_Base):
    def test_remote_addr_is_the_answer(self):
        self.assertEqual(client_ip(req("8.8.8.8")), "8.8.8.8")

    def test_x_forwarded_for_is_ignored_entirely(self):
        self.assertEqual(
            client_ip(req("8.8.8.8", xff="198.51.100.1")), "8.8.8.8"
        )

    def test_no_remote_addr_is_empty(self):
        self.assertEqual(client_ip(RequestFactory().get("/", REMOTE_ADDR="")), "")

    def test_an_unparseable_remote_addr_is_returned_as_is(self):
        self.assertEqual(client_ip(req("unix:")), "unix:")

    def test_ipv4_mapped_ipv6_is_normalised(self):
        self.assertEqual(client_ip(req("::ffff:8.8.8.8")), "8.8.8.8")


@override_settings(OSDS_TRUSTED_PROXIES=TRUSTED)
class TrustedProxyTests(_Base):
    def test_the_client_is_the_address_the_proxy_saw(self):
        self.assertEqual(client_ip(req(PROXY, xff="198.51.100.1")), "198.51.100.1")

    def test_a_peer_that_is_not_a_trusted_proxy_cannot_assert_a_client(self):
        self.assertEqual(
            client_ip(req("8.8.8.8", xff="198.51.100.1")), "8.8.8.8"
        )

    def test_a_trusted_proxy_with_no_header_is_its_own_answer(self):
        self.assertEqual(client_ip(req(PROXY)), PROXY)

    def test_a_chain_of_trusted_proxies_is_walked_from_the_right(self):
        self.assertEqual(
            client_ip(req(PROXY, xff="198.51.100.1, 10.0.0.9, 10.0.0.7")),
            "198.51.100.1",
        )

    def test_a_forged_leading_entry_is_not_believed(self):
        # The client sent "1.2.3.4"; the first proxy appended the real peer.
        self.assertEqual(
            client_ip(req(PROXY, xff="1.2.3.4, 198.51.100.1")), "198.51.100.1"
        )

    def test_a_forged_entry_behind_an_untrusted_hop_is_not_believed(self):
        # 198.51.100.1 is untrusted, so the walk stops there; "9.9.9.9" to its
        # left was written by that host, not by our proxy.
        self.assertEqual(
            client_ip(req(PROXY, xff="9.9.9.9, 198.51.100.1, 10.0.0.9")),
            "198.51.100.1",
        )

    def test_when_every_hop_is_trusted_the_leftmost_is_the_client(self):
        self.assertEqual(client_ip(req(PROXY, xff="10.1.1.1, 10.2.2.2")), "10.1.1.1")

    def test_a_garbage_entry_stops_the_walk_at_the_last_good_hop(self):
        self.assertEqual(client_ip(req(PROXY, xff="garbage, 10.2.2.2")), "10.2.2.2")

    def test_a_port_suffix_is_tolerated(self):
        self.assertEqual(
            client_ip(req(PROXY, xff="198.51.100.1:51234")), "198.51.100.1"
        )
        self.assertEqual(
            client_ip(req(PROXY, xff="[2001:db8::1]:51234")), "2001:db8::1"
        )

    def test_ipv6_proxies_and_clients(self):
        self.assertEqual(
            client_ip(req("fd00::1", xff="2001:db8::7")), "2001:db8::7"
        )

    def test_an_empty_header_is_the_proxy(self):
        self.assertEqual(client_ip(req(PROXY, xff="")), PROXY)

    def test_an_ipv4_proxy_net_does_not_match_an_ipv6_peer(self):
        self.assertEqual(
            client_ip(req("2001:db8::1", xff="198.51.100.1")), "2001:db8::1"
        )


class BucketTests(SimpleTestCase):
    def test_ipv4_is_the_address(self):
        self.assertEqual(ip_bucket("8.8.8.8"), "8.8.8.8")

    def test_ipv6_is_the_slash_64(self):
        self.assertEqual(ip_bucket("2001:db8:1:2:aaaa:bbbb:cccc:dddd"), "2001:db8:1:2::")

    def test_a_mapped_address_is_its_ipv4(self):
        self.assertEqual(ip_bucket("::ffff:8.8.8.8"), "8.8.8.8")

    def test_unusable_input_is_empty(self):
        for value in ("", "nope", None):
            self.assertEqual(ip_bucket(value), "")


class ParseNetworksTests(SimpleTestCase):
    def test_accepts_addresses_and_cidrs_and_normalises(self):
        self.assertEqual(
            parse_networks(" 10.0.0.0/8 , 172.18.0.2, fd00::/8,, "),
            ("10.0.0.0/8", "172.18.0.2/32", "fd00::/8"),
        )

    def test_empty_means_trust_nobody(self):
        self.assertEqual(parse_networks(""), ())
        self.assertEqual(parse_networks("  "), ())

    def test_a_typo_stops_the_process_rather_than_trusting_nothing_quietly(self):
        with self.assertRaisesMessage(ValueError, "OSDS_TRUSTED_PROXIES"):
            parse_networks("10.0.0.0/8, not-a-network")

    def test_the_setting_defaults_to_nobody(self):
        from django.conf import settings

        self.assertEqual(settings.OSDS_TRUSTED_PROXIES, ())


class MisconfiguredProxyWarningTests(_Base):
    LOGGER = "osds.client_ip"

    def test_warns_once_for_a_private_peer_sending_x_forwarded_for(self):
        with self.assertLogs(self.LOGGER, "WARNING") as logs:
            client_ip(req("172.18.0.2", xff="198.51.100.1"))
            client_ip(req("172.18.0.2", xff="198.51.100.2"))
        self.assertEqual(len(logs.records), 1)
        self.assertIn("OSDS_TRUSTED_PROXIES", logs.output[0])

    def test_still_answers_with_the_peer_and_disables_nothing(self):
        with self.assertLogs(self.LOGGER, "WARNING"):
            result = client_ip(req("172.18.0.2", xff="198.51.100.1"))
        self.assertEqual(result, "172.18.0.2")

    def test_silent_without_an_x_forwarded_for_header(self):
        with self.assertNoLogs(self.LOGGER, "WARNING"):
            client_ip(req("172.18.0.2"))

    def test_silent_for_a_public_peer(self):
        with self.assertNoLogs(self.LOGGER, "WARNING"):
            client_ip(req("8.8.8.8", xff="198.51.100.1"))

    def test_silent_for_loopback(self):
        with self.assertNoLogs(self.LOGGER, "WARNING"):
            client_ip(req("127.0.0.1", xff="198.51.100.1"))

    @override_settings(OSDS_TRUSTED_PROXIES=TRUSTED)
    def test_silent_when_proxies_are_configured(self):
        with self.assertNoLogs(self.LOGGER, "WARNING"):
            client_ip(req(PROXY, xff="198.51.100.1"))
