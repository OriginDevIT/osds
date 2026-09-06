"""Host resolution.

``TenantResolutionMiddleware`` is the FIRST middleware in the stack. With
``ALLOWED_HOSTS = ['*']`` nothing else validates the Host header, and
``SecurityMiddleware``'s SSL redirect would otherwise build a redirect URL from
an unvalidated host. This middleware is the authoritative host gate: it decides
whether a request is for the installation console, a tenant's own domain, or
neither, and it establishes (and always tears down) the ambient tenant scope
that ``osds.db.TenantScopedManager`` reads.

The first-run branch (route everything to the setup wizard until setup is
complete) is added with the wizard.
"""

from __future__ import annotations

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotFound

from osds.tenancy import reset_current_tenant, set_current_tenant
from tenants.models import Tenant


class TenantResolutionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = self._host(request)
        kind, tenant, early = self._resolve(host)

        request.osds_host_kind = kind
        request.tenant = tenant
        if kind == "console":
            request.urlconf = "osds.urls_console"
        elif kind == "tenant":
            request.urlconf = "osds.urls_tenant"

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

    def _resolve(self, host: str):
        """Return ``(kind, tenant, early_response)``.

        ``early_response`` is non-None only for hosts that get a fixed response
        without reaching a view: unknown (404) and suspended (503).
        """
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
