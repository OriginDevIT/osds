"""The inbound route a payment provider calls (decisions.md §4.11).

``POST /_adapters/<adapter_id>/inbound/`` on a tenant's own host. The tenant is
the host's, so a webhook URL is per directory and a secret is per directory.
The route is CSRF-exempt by necessity -- the caller is a provider's server --
and is authenticated by the adapter, which verifies the provider's signature
over the *raw* body before it returns anything.

Core does three things only: finds the adapter that registered itself under
that id, hands it the raw request through ``AdapterContext``, and applies the
``PaymentReport``s it returns through ``entitlement.reportPayment``. A request
the adapter rejects (a bad signature) is answered with the adapter's own status
and nothing is applied. A report core cannot apply is acknowledged -- the
provider is told it arrived -- and recorded ``ignored``: retrying cannot fix it.
A failure that retrying *can* fix (the database, an unconfigured secret) is a
5xx, so the provider tries again.
"""

from __future__ import annotations

import json
from datetime import timedelta

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from audit.ratelimit import Rule, check
from billing import reports
from osds.adapter_api import InboundRequest, SecretNotFound
from osds.adapter_context import build_context
from osds.adapters import adapter_by_id
from osds.client_ip import client_ip

MAX_BODY = 256 * 1024
# A provider's burst is bounded by its own retry schedule; this is a ceiling on
# an unauthenticated endpoint that does a signature check per request.
INBOUND_IP = (Rule("adapter.inbound.ip.1m", 300, timedelta(minutes=1)),)


def _headers(request) -> dict:
    out = {}
    for key, value in request.META.items():
        if key.startswith("HTTP_"):
            out[key[5:].replace("_", "-").lower()] = value
    for key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
        if request.META.get(key):
            out[key.replace("_", "-").lower()] = request.META[key]
    return out


@csrf_exempt
@require_POST
def adapter_inbound(request, adapter_id):
    tenant = getattr(request, "tenant", None)
    if getattr(request, "osds_host_kind", None) != "tenant" or tenant is None:
        return HttpResponse(status=404)
    provider = adapter_by_id(adapter_id)
    if provider is None or not hasattr(provider, "inbound"):
        return HttpResponse(status=404)

    now = timezone.now()
    verdict = check(tenant, checks=[("ip", client_ip(request), INBOUND_IP)], now=now)
    if not verdict.allowed:
        response = HttpResponse("rate limited", status=429)
        response["Retry-After"] = str(verdict.retry_after)
        return response

    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length > MAX_BODY:
        return HttpResponse(status=413)
    body = request.body
    if len(body) > MAX_BODY:
        return HttpResponse(status=413)

    try:
        result = provider.inbound(
            build_context(tenant, provider),
            InboundRequest(method=request.method, headers=_headers(request), body=body),
        )
    except SecretNotFound:
        return HttpResponse("not configured", status=503)

    if result.status != 200:
        return HttpResponse(result.body, status=result.status, content_type="text/plain")

    counts = {"applied": 0, "replayed": 0, "ignored": 0}
    for report in result.reports:
        outcome = reports.report_payment(tenant, adapter_id=adapter_id, report=report, now=now)
        counts[outcome.outcome] += 1
    return JsonResponse({"received": len(result.reports), **counts})
