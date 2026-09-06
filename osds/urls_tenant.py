"""URLconf served on a tenant's own domain: the domain-verification
challenge, the tenant admin, and the public directory site.

The public site is a single catch-all (``public_dispatch``) because routing
depends on the tenant's live listing-type count -- see directory/public_views.
``robots.txt`` and the sitemap arrive in PR 4.
"""

from django.http import HttpResponse
from django.urls import include, path, re_path

from directory import public_views

handler404 = "directory.public_views.not_found"


def _challenge(request):
    token = ""
    tenant = getattr(request, "tenant", None)
    if tenant is not None:
        token = (tenant.settings or {}).get("domain_challenge", "")
    return HttpResponse(token, content_type="text/plain; charset=utf-8")


urlpatterns = [
    path(".well-known/osds-challenge", _challenge, name="domain-challenge"),
    path("admin/", include("directory.admin_urls")),
    path("search/", public_views.search_results, name="public-search"),
    path("", public_views.home, name="public-home"),
    re_path(r"^(?P<path>.+)$", public_views.public_dispatch, name="public-dispatch"),
]
