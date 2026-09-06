"""URLconf served on a tenant's own domain.

The public directory site lands here in later PRs. For now it serves the
domain-verification challenge and the tenant admin.
"""

from django.http import HttpResponse
from django.urls import include, path


def _challenge(request):
    token = ""
    tenant = getattr(request, "tenant", None)
    if tenant is not None:
        token = (tenant.settings or {}).get("domain_challenge", "")
    return HttpResponse(token, content_type="text/plain; charset=utf-8")


urlpatterns = [
    path(".well-known/osds-challenge", _challenge, name="domain-challenge"),
    path("admin/", include("directory.admin_urls")),
]
