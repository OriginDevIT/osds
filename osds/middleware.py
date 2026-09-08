"""Host resolution.

``TenantResolutionMiddleware`` is the FIRST middleware in the stack. With
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

from osds.tenancy import reset_current_tenant, set_current_tenant
from osds.urlconf import CONSOLE_URLCONF, SETUP_URLCONF, TENANT_URLCONF
from tenants.dns_check import CHALLENGE_PATH
from tenants.models import Tenant
from tenants.setup_state import setup_complete


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
