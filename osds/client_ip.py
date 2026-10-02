"""The client's IP address, and what to do about proxies (#210, decisions.md §4.8).

``client_ip(request)`` is the one answer to "who sent this", used by the claim
rate limiter and by the consent record. It trusts nothing a client can write:

* With no trusted proxies configured -- the default, and what a directly
  exposed install is -- the answer is ``REMOTE_ADDR`` and ``X-Forwarded-For`` is
  ignored. A client can send any header it likes.
* With ``OSDS_TRUSTED_PROXIES`` set (comma-separated addresses or CIDRs), and
  only when ``REMOTE_ADDR`` is inside it, ``X-Forwarded-For`` is read from the
  right: each hop that is itself a trusted proxy is skipped, and the first
  address that is not is the client. This is safe against a proxy that appends
  to a client-supplied header (nginx's ``$proxy_add_x_forwarded_for``) and
  against a client that prepends fake entries, because fake entries sit to the
  left of everything a trusted proxy wrote.

Behind a proxy with ``OSDS_TRUSTED_PROXIES`` unset, every visitor appears to
come from the proxy. Per-IP limits then act as one site-wide limit. The module
logs one WARNING per process when it sees that shape; it does not disable
anything (a limit that silently turns off, or one that locks everyone out, are
both worse than a log line the operator can act on).
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable

from django.conf import settings

logger = logging.getLogger("osds.client_ip")

_warned = False


def parse_networks(value: str) -> "tuple[str, ...]":
    """Validate ``OSDS_TRUSTED_PROXIES``: comma-separated addresses or CIDRs.
    Raises ``ValueError`` on anything else, so a typo stops the process at boot
    instead of quietly trusting nothing (or everything)."""
    networks = []
    for token in (value or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            networks.append(str(ipaddress.ip_network(token, strict=False)))
        except ValueError as exc:
            raise ValueError(
                f"OSDS_TRUSTED_PROXIES: {token!r} is not an IP address or CIDR"
            ) from exc
    return tuple(networks)


def _parse(token: str):
    """An ``ip_address`` for ``token``, or ``None``. Tolerates ``host:port`` and
    ``[v6]:port`` (some proxies append the port) and IPv4-mapped IPv6."""
    token = (token or "").strip()
    if not token:
        return None
    candidates = [token]
    if token.startswith("[") and "]" in token:
        candidates.append(token[1 : token.index("]")])
    elif token.count(":") == 1:
        candidates.append(token.split(":", 1)[0])
    for candidate in candidates:
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        mapped = getattr(addr, "ipv4_mapped", None)
        return mapped or addr
    return None


def _trusted_networks() -> list:
    return [
        ipaddress.ip_network(n)
        for n in getattr(settings, "OSDS_TRUSTED_PROXIES", ()) or ()
    ]


def _is_trusted(addr, networks: Iterable) -> bool:
    return any(addr.version == net.version and addr in net for net in networks)


def _warn_once(peer, request) -> None:
    global _warned
    if _warned or not request.META.get("HTTP_X_FORWARDED_FOR"):
        return
    if peer.is_loopback or not peer.is_private:
        return
    _warned = True
    logger.warning(
        "A request from the private address %s carried X-Forwarded-For, but "
        "OSDS_TRUSTED_PROXIES is not set, so every visitor behind that proxy is "
        "treated as one client and the claim rate limits act site-wide. Set "
        "OSDS_TRUSTED_PROXIES to the proxy's address or network.",
        peer,
    )


def client_ip(request) -> str:
    """The client's address as text, or ``""`` when there is none."""
    remote = request.META.get("REMOTE_ADDR", "") or ""
    peer = _parse(remote)
    if peer is None:
        return remote

    networks = _trusted_networks()
    if not networks:
        _warn_once(peer, request)
        return str(peer)
    if not _is_trusted(peer, networks):
        return str(peer)

    addr = peer
    header = request.META.get("HTTP_X_FORWARDED_FOR", "") or ""
    for token in reversed(header.split(",")):
        if not _is_trusted(addr, networks):
            break
        parsed = _parse(token)
        if parsed is None:
            break
        addr = parsed
    return str(addr)


def ip_bucket(ip: str) -> str:
    """The rate-limit key for ``ip``: the address for IPv4, the /64 for IPv6
    (one subscriber commonly holds a whole /64, so per-address would be no
    limit at all). ``""`` for anything unparseable."""
    addr = _parse(ip)
    if addr is None:
        return ""
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False).network_address)
    return str(addr)
