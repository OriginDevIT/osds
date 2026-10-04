"""Building an ``AdapterContext`` (decisions.md §4.11, §4.12, #215, #179).

Core is the only place that reads a tenant's adapter config and secrets and
the only place that opens a socket on an adapter's behalf. The adapter gets
callables, never the tenant row, never ``tenants.secrets`` and never a raw
``urllib``.

* **Config** lives at ``tenant.settings["adapters"][adapter_id]``, unless the
  provider names a ``config_key`` -- the SMTP settings predate the adapter
  runtime and stay at ``tenant.settings["smtp"]``.
* **Secrets** resolve tenant override then deployment (spec §8.1), stored under
  ``"<adapter_id>_<key>"``. Every resolved value is remembered so that
  ``ctx.logger`` can scrub it from anything logged.
* **Egress** has two shapes. A provider with a fixed ``egress_allowlist`` (a
  payment provider's API host) is held to exact HTTPS hostnames. A provider
  whose destination is the operator's own URL names it through
  ``egress_allowlist_for(config)`` and is held to the stricter rules in
  ``make_operator_http``: the destination must resolve to public addresses
  (unless the deployment opted in to private ones), the connection is pinned to
  the address that was checked, and the response is capped and deadline-bound.
  Redirects are never followed for either: a redirect to an unlisted host would
  otherwise slip past the check.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone

from osds.adapter_api import AdapterContext, EgressDenied, HttpResponse, SecretNotFound
from osds.adapter_logging import make_adapter_logger

_TIMEOUT = 30  # spec §8.2 handler timeout

# The operator-URL fetch must finish inside the drain's claim visibility
# deadline (90s) with room to spare: the per-operation socket timeout alone
# would let a server that drips a byte every few seconds hold a delivery open
# far past it.
CONNECT_TIMEOUT = 10
TOTAL_DEADLINE = 25
MAX_RESPONSE_BYTES = 64 * 1024


def secret_name(adapter_id: str, key: str) -> str:
    return f"{adapter_id}_{key}"


def provider_id(provider) -> str:
    """A payment provider has ``adapter_id``; an event subscriber has ``id``."""
    return getattr(provider, "adapter_id", None) or provider.id


def adapter_config(tenant, adapter_id: str, *, config_key: "str | None" = None) -> dict:
    stored = tenant.settings or {}
    if config_key:
        return dict(stored.get(config_key) or {})
    return dict((stored.get("adapters") or {}).get(adapter_id) or {})


def scope_setting(scope: str) -> str:
    """The config key holding a scope's grant: ``pii:contact`` ->
    ``scope_pii_contact``. A scalar, because adapter config accepts nothing else."""
    return "scope_" + scope.replace(":", "_")


def granted_scopes(subscriber, tenant) -> "frozenset[str]":
    """What a subscriber may see for ``tenant``: the scopes it asks for that the
    tenant's admin has turned on. A subscriber that asks for none, or a delivery
    with no tenant (``tenant.*``), gets none -- everything contact-like is redacted."""
    requested = frozenset(getattr(subscriber, "scopes", ()) or ())
    if tenant is None or not requested:
        return frozenset()
    config = adapter_config(
        tenant, provider_id(subscriber), config_key=getattr(subscriber, "config_key", None)
    )
    return frozenset(s for s in requested if config.get(scope_setting(s)) is True)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # pragma: no cover - trivial
        return None


def make_http(allowlist, *, opener=None):
    """An HTTP callable that only reaches ``allowlist`` (hostnames, exact
    match, HTTPS only). ``opener`` is injectable for tests."""
    allowed = {h.lower() for h in allowlist}
    opener = opener or urllib.request.build_opener(_NoRedirects)

    def fetch(method, url, *, headers=None, body=None, timeout=_TIMEOUT) -> HttpResponse:
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").lower() not in allowed:
            raise EgressDenied(f"{parts.hostname or url!r} is not on this adapter's allowlist")
        request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with opener.open(request, timeout=timeout) as response:
                return HttpResponse(
                    response.status, response.read(),
                    {k.lower(): v for k, v in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read(), {k.lower(): v for k, v in exc.headers.items()})

    return fetch


# --- operator-supplied destinations -------------------------------------------

# Never reachable, whatever the deployment allows. 169.254.0.0/16 holds the
# cloud metadata service (169.254.169.254) and every link-local address; the
# rest are addresses that are never a real peer or never a safe one.
_ALWAYS_DENIED = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4",
        "::/128", "::1/128", "fe80::/10", "ff00::/8", "fd00:ec2::254/128",
    )
]


def classify_address(raw: str) -> str:
    """``"denied"`` (never), ``"private"`` (only with the deployment opt-in) or
    ``"public"``. An IPv4-mapped IPv6 address is judged as the IPv4 it wraps."""
    addr = ipaddress.ip_address(raw)
    mapped = getattr(addr, "ipv4_mapped", None)
    addr = mapped or addr
    if any(addr.version == net.version and addr in net for net in _ALWAYS_DENIED):
        return "denied"
    return "public" if addr.is_global else "private"


def check_destination(scheme: str, addresses, *, allow_private: bool) -> None:
    """Raise ``EgressDenied`` unless every address the host resolves to is
    acceptable. All of them, not the first: a name that resolves to one public
    and one internal address is exactly the case to refuse."""
    if not addresses:
        raise EgressDenied("the host did not resolve")
    kinds = {classify_address(a) for a in addresses}
    if "denied" in kinds:
        raise EgressDenied("the host resolves to a blocked address")
    if "private" in kinds and not allow_private:
        raise EgressDenied("the host resolves to a private address")
    if scheme == "http" and kinds != {"private"}:
        raise EgressDenied("plain HTTP is only allowed to private addresses")


def resolve_host(host: str) -> "list[str]":
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EgressDenied("the host did not resolve") from exc
    seen: "list[str]" = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connects to a given IP while the request still names the host."""

    def __init__(self, host, port, *, ip, **kwargs):
        super().__init__(host, port, **kwargs)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """As above, with the certificate checked against the *hostname*, not the
    address -- so pinning to the checked IP costs nothing in verification."""

    def __init__(self, host, port, *, ip, **kwargs):
        super().__init__(host, port, **kwargs)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def pinned_transport(*, scheme, host, port, ip, method, target, headers, body, timeout, deadline):
    """The default transport: one request, to ``ip``, with a size cap and a
    total deadline (``time.monotonic`` seconds)."""
    if scheme == "https":
        conn = _PinnedHTTPSConnection(
            host, port, ip=ip, timeout=timeout, context=ssl.create_default_context()
        )
    else:
        conn = _PinnedHTTPConnection(host, port, ip=ip, timeout=timeout)
    try:
        conn.request(method, target, body=body, headers=headers)
        response = conn.getresponse()
        chunks, size = [], 0
        while size <= MAX_RESPONSE_BYTES:
            if time.monotonic() > deadline:
                raise TimeoutError("the response took too long")
            chunk = response.read(8192)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return HttpResponse(
            response.status,
            b"".join(chunks)[:MAX_RESPONSE_BYTES],
            {k.lower(): v for k, v in response.getheaders()},
        )
    finally:
        conn.close()


def make_operator_http(allowed_hosts, *, allow_private=False, resolver=None, transport=None):
    """An HTTP callable for a destination the operator chose.

    ``allowed_hosts`` is the host the configuration names; the URL passed to the
    callable must be exactly that host (a URL changed without its config, or the
    reverse, is refused). Beyond that: ``http`` only to private addresses and
    only with ``allow_private``; no credentials in the URL; every resolved
    address checked; the connection pinned to the first one. ``resolver`` and
    ``transport`` are injectable for tests."""
    allowed = {h.lower() for h in allowed_hosts if h}
    resolver = resolver or resolve_host
    transport = transport or pinned_transport

    def fetch(method, url, *, headers=None, body=None, timeout=_TIMEOUT) -> HttpResponse:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("https", "http") or not host or host not in allowed:
            raise EgressDenied("the destination is not the configured host")
        if parts.username or parts.password:
            raise EgressDenied("credentials in a URL are not accepted")
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError as exc:
            raise EgressDenied("the destination has an invalid port") from exc
        addresses = [host] if _is_ip(host) else resolver(host)
        check_destination(parts.scheme, addresses, allow_private=allow_private)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        deadline = time.monotonic() + TOTAL_DEADLINE
        return transport(
            scheme=parts.scheme, host=host, port=port, ip=addresses[0], method=method,
            target=target, headers=dict(headers or {}), body=body,
            timeout=min(timeout, CONNECT_TIMEOUT), deadline=deadline,
        )

    return fetch


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


# --- the context ------------------------------------------------------------------


def build_context(tenant, provider, *, opener=None, resolver=None, transport=None) -> AdapterContext:
    from tenants.secrets import ConfigurationError, get_secret

    adapter_id = provider_id(provider)
    config = adapter_config(
        tenant, adapter_id, config_key=getattr(provider, "config_key", None)
    )
    resolved: "set[str]" = set()

    def secret(key: str) -> str:
        try:
            value = get_secret(secret_name(adapter_id, key), tenant=tenant)
        except ConfigurationError as exc:
            raise SecretNotFound(key) from exc
        resolved.add(value)
        return value

    destination = getattr(provider, "egress_allowlist_for", None)
    if destination is not None:
        http = make_operator_http(
            destination(config),
            allow_private=bool(getattr(settings, "OSDS_WEBHOOK_ALLOW_PRIVATE", False)),
            resolver=resolver,
            transport=transport,
        )
    else:
        http = make_http(getattr(provider, "egress_allowlist", ()), opener=opener)

    return AdapterContext(
        tenant_id=tenant.public_id,
        tenant_slug=tenant.slug,
        tenant_domain=tenant.primary_domain,
        config=config,
        secret=secret,
        http=http,
        clock=timezone.now,
        logger=make_adapter_logger(adapter_id, resolved),
    )
