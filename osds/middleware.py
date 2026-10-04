"""Host resolution.

``TenantResolutionMiddleware`` is the first middleware to look at the Host
(``TrustedProxyHeadersMiddleware`` sits above it and reads only the peer and
``X-Forwarded-Proto``, never the Host). With
``ALLOWED_HOSTS = ['*']`` nothing else validates the Host header, and
``SecurityMiddleware``'s SSL redirect would otherwise build a redirect URL from
an unvalidated host. This middleware is the authoritative host gate: it decides
whether a request is for the installation console, a tenant's own domain, or
neither, and it establishes (and always tears down) the ambient tenant scope
that ``osds.db.TenantScopedManager`` reads.

While first-run setup is incomplete every host routes to the wizard
(``osds.urls_setup``), with one exception: the domain-verification challenge
endpoint still answers so the wizard's own HTTP check can pass.
"""

from __future__ import annotations

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotFound

from osds.client_ip import peer_is_trusted_proxy, warn_untrusted_proto
from osds.tenancy import reset_current_tenant, set_current_tenant
from osds.urlconf import CONSOLE_URLCONF, SETUP_URLCONF, TENANT_URLCONF
from tenants.dns_check import CHALLENGE_PATH
from tenants.models import Tenant
from tenants.setup_state import setup_complete
from tenants.tls_ask import ask as tls_ask, is_ask_request


class TrustedProxyHeadersMiddleware:
    """Believe ``X-Forwarded-Proto`` only from a trusted proxy (#234,
    decisions.md §4.13).

    ``SECURE_PROXY_SSL_HEADER`` makes Django read the scheme from that header
    whoever sent it. This runs first, before anything asks ``request.scheme``:

    * the peer is not in ``OSDS_TRUSTED_PROXIES`` (or none are configured): the
      header is deleted, and the scheme is whatever the server saw;
    * the peer is a trusted proxy: the header is cut to its rightmost token.
      Django reads the leftmost, which is client-written when a proxy appends,
      and the rightmost is the one the trusted proxy wrote -- the same
      right-to-left rule ``osds.client_ip`` applies to ``X-Forwarded-For``.
    """

    HEADER = "HTTP_X_FORWARDED_PROTO"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        value = request.META.get(self.HEADER)
        if value is not None:
            if peer_is_trusted_proxy(request):
                request.META[self.HEADER] = value.rsplit(",", 1)[-1].strip()
            else:
                del request.META[self.HEADER]
                warn_untrusted_proto(request, value)
        return self.get_response(request)


class SetupCookieSecurityMiddleware:
    """Drop the ``Secure`` flag from the session and CSRF cookies on first-run
    wizard responses.

    ``SESSION_COOKIE_SECURE`` and ``CSRF_COOKIE_SECURE`` default on
    (``OSDS_SECURE_COOKIES``). The wizard, though, is reached over plain http
    on a bare IP before any TLS exists, and a browser drops a ``Secure`` cookie
    set from an insecure origin -- so the wizard could neither keep its unlock
    flag in the session nor pass CSRF, and first boot would be impossible.

    The gate is the route plus setup being incomplete. There is no reference to
    ``request.is_secure()``: outside ``/setup/``, and the moment an operator
    completes the wizard, ``Secure`` is left untouched. The setup URLconf stops
    resolving at that point, so the relaxation self-destructs.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._cookie_names = (
            settings.SESSION_COOKIE_NAME,
            settings.CSRF_COOKIE_NAME,
        )

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/setup/") and not setup_complete():
            for name in self._cookie_names:
                morsel = response.cookies.get(name)
                if morsel is not None:
                    morsel["secure"] = ""
        return response


class TenantResolutionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # The TLS proxy's own ask call (decisions.md §4.13) carries an internal
        # Host no tenant owns, so it is answered before the Host is read. It
        # sets no urlconf and no tenant scope; anyone else falls through.
        if is_ask_request(request):
            request.osds_host_kind = "internal"
            request.tenant = None
            return tls_ask(request)

        host = self._host(request)
        kind, tenant, early = self._resolve(request, host)

        request.osds_host_kind = kind
        request.tenant = tenant
        if kind == "console":
            request.urlconf = CONSOLE_URLCONF
        elif kind == "tenant":
            request.urlconf = TENANT_URLCONF
        elif kind == "setup":
            request.urlconf = SETUP_URLCONF

        token = set_current_tenant(tenant)
        try:
            if early is not None:
                return early
            return self.get_response(request)
        finally:
            reset_current_tenant(token)

    @staticmethod
    def _host(request) -> str:
        # request.get_host() raises DisallowedHost on a malformed header even
        # with ALLOWED_HOSTS=['*']; Django turns that into a 400 around us.
        return request.get_host().split(":", 1)[0].strip().rstrip(".").lower()

    def _resolve(self, request, host: str):
        """Return ``(kind, tenant, early_response)``.

        ``early_response`` is non-None only for hosts that get a fixed response
        without reaching a view: unknown (404) and suspended (503).
        """
        if not setup_complete():
            # The challenge endpoint must answer mid-setup so the wizard's HTTP
            # domain check can pass; everything else goes to the wizard.
            if request.path == CHALLENGE_PATH:
                tenant = self._lookup_tenant(host)
                if tenant is not None:
                    return "tenant", tenant, None
            return "setup", None, None

        console_host = (getattr(settings, "OSDS_CONSOLE_HOST", "") or "").lower()
        if console_host and host == console_host:
            # Console wins over a tenant that somehow claims the same name.
            return "console", None, None

        tenant = self._lookup_tenant(host)
        if tenant is not None:
            if tenant.status == Tenant.Status.SUSPENDED:
                return "suspended", None, self._suspended()
            # Routing keys on primary_domain regardless of domain verification.
            return "tenant", tenant, None

        # DEBUG-only: lets a developer without DNS reach a directory on
        # localhost. Inert in production, where DEBUG is off.
        if settings.DEBUG:
            dev_tenant = self._dev_tenant()
            if dev_tenant is not None:
                return "tenant", dev_tenant, None

        return "unknown", None, self._not_found(host)

    @staticmethod
    def _lookup_tenant(host: str):
        if not host:
            return None
        # No cache: one lookup on the unique primary_domain index per request.
        return Tenant.objects.filter(primary_domain=host).first()

    @staticmethod
    def _dev_tenant():
        # Callers gate on settings.DEBUG; this re-checks so it can never fire
        # from a stray call.
        if not settings.DEBUG:
            return None
        slug = getattr(settings, "OSDS_DEV_TENANT_SLUG", "") or ""
        if not slug:
            return None
        return Tenant.objects.filter(
            slug=slug, status=Tenant.Status.ACTIVE
        ).first()

    @staticmethod
    def _not_found(host: str) -> HttpResponseNotFound:
        return HttpResponseNotFound(
            f"No directory is configured for {host!r}.\n",
            content_type="text/plain; charset=utf-8",
        )

    @staticmethod
    def _suspended() -> HttpResponse:
        return HttpResponse(
            "This directory is suspended.\n",
            status=503,
            content_type="text/plain; charset=utf-8",
        )
