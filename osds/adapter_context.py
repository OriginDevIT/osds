"""Building an ``AdapterContext`` (decisions.md §4.11, #215).

Core is the only place that reads a tenant's adapter config and secrets and
the only place that opens a socket on an adapter's behalf. The adapter gets
callables, never the tenant row, never ``tenants.secrets`` and never a raw
``urllib``.

* **Config** lives at ``tenant.settings["adapters"][adapter_id]``.
* **Secrets** resolve tenant override then deployment (spec §8.1), stored under
  ``"<adapter_id>_<key>"``.
* **Egress** is allowlisted by hostname: a request to any other host raises
  ``EgressDenied`` before a socket is opened, and redirects are not followed
  (a redirect to an unlisted host would otherwise slip past the check).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from urllib.parse import urlsplit

from django.utils import timezone

from osds.adapter_api import AdapterContext, EgressDenied, HttpResponse, SecretNotFound

_TIMEOUT = 30  # spec §8.2 handler timeout


def secret_name(adapter_id: str, key: str) -> str:
    return f"{adapter_id}_{key}"


def adapter_config(tenant, adapter_id: str) -> dict:
    return dict(((tenant.settings or {}).get("adapters") or {}).get(adapter_id) or {})


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


def build_context(tenant, provider, *, opener=None) -> AdapterContext:
    from tenants.secrets import ConfigurationError, get_secret

    adapter_id = provider.adapter_id

    def secret(key: str) -> str:
        try:
            return get_secret(secret_name(adapter_id, key), tenant=tenant)
        except ConfigurationError as exc:
            raise SecretNotFound(key) from exc

    return AdapterContext(
        tenant_id=tenant.public_id,
        tenant_slug=tenant.slug,
        tenant_domain=tenant.primary_domain,
        config=adapter_config(tenant, adapter_id),
        secret=secret,
        http=make_http(getattr(provider, "egress_allowlist", ()), opener=opener),
        clock=timezone.now,
    )
