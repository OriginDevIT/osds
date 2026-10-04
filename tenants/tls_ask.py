"""The on-demand TLS ask endpoint (decisions.md §4.13).

A TLS-terminating proxy that issues certificates on demand (Caddy's
``on_demand_tls { ask ... }``) calls ``GET /_internal/tls-ask?domain=<name>``
before it requests a certificate for a name it has never seen. 200 means yes,
anything else means no. Without an answer a stranger could point any DNS name
at the server and make it burn certificate-authority rate limits.

The answer is yes for a tenant's ``primary_domain`` whose domain is verified
(a suspended tenant included: its 503 page needs HTTPS to be read), and for
``OSDS_CONSOLE_HOST``. Nothing else.

``TenantResolutionMiddleware`` calls ``is_ask_request`` before it touches the
Host header, because the proxy's own call carries an internal Host that no
tenant owns. The endpoint is reachable only when the socket peer is in
``OSDS_TRUSTED_PROXIES`` *and* the request carries no forwarding header: public
traffic the proxy relayed always has one (the proxy's own ask call never does),
so a visitor cannot reach the endpoint through the proxy. Every other request
falls through to the ordinary 404, so a stranger cannot tell the path exists.
"""

from __future__ import annotations

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotFound

from osds.client_ip import peer_is_trusted_proxy
from tenants.models import Tenant
from tenants.wizard.forms import _HOSTNAME_RE

TLS_ASK_PATH = "/_internal/tls-ask"

# Headers a proxy adds to traffic it relays. The proxy's own ask call has none.
FORWARDING_HEADERS = (
    "HTTP_X_FORWARDED_FOR",
    "HTTP_X_FORWARDED_HOST",
    "HTTP_X_FORWARDED_PROTO",
    "HTTP_X_FORWARDED_PORT",
    "HTTP_FORWARDED",
    "HTTP_X_REAL_IP",
)


def is_ask_request(request) -> bool:
    """Whether ``request`` is the proxy's own ask call, to be answered here."""
    if request.path != TLS_ASK_PATH or request.method not in ("GET", "HEAD"):
        return False
    if any(name in request.META for name in FORWARDING_HEADERS):
        return False
    return peer_is_trusted_proxy(request)


def domain_may_get_certificate(domain: str) -> bool:
    domain = (domain or "").strip().rstrip(".").lower()
    if not _HOSTNAME_RE.match(domain):
        return False
    console_host = (getattr(settings, "OSDS_CONSOLE_HOST", "") or "").strip().rstrip(".").lower()
    if console_host and domain == console_host:
        return True
    # Tenant is a structural model, not tenant data: no ambient scope applies.
    return Tenant.objects.filter(
        primary_domain=domain, domain_verified_at__isnull=False
    ).exists()


def ask(request):
    if domain_may_get_certificate(request.GET.get("domain", "")):
        response = HttpResponse("ok\n", content_type="text/plain; charset=utf-8")
    else:
        response = HttpResponseNotFound()
    response["Cache-Control"] = "no-store"
    return response
