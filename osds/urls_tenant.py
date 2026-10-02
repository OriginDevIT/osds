"""URLconf served on a tenant's own domain: the domain-verification
challenge, the tenant admin, and the public directory site.

The public site is a single catch-all (``public_dispatch``) because routing
depends on the tenant's live listing-type count -- see directory/public_views.
``robots.txt`` and the sitemap index have their own routes, ahead of the
catch-all.
"""

from django.http import HttpResponse
from django.urls import include, path, re_path
from django.views.generic.base import RedirectView

from directory import claim_views, lead_views, public_views

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
    # The wizard's completion screen tells the operator to sign in at
    # "<domain>/admin"; without this the greedy public catch-all below swallows
    # the slashless form and returns a 404 instead of the login redirect.
    # query_string=True carries a bookmarked "?next=" through to /admin/.
    path(
        "admin",
        RedirectView.as_view(url="/admin/", permanent=False, query_string=True),
    ),
    path("search/", public_views.search_results, name="public-search"),
    # Local media store. Matched ahead of the public catch-all; "media" is a
    # reserved slug so no listing or category can shadow it.
    path("media/<str:public_id>", public_views.media_asset, name="media-asset"),
    # Claim submission (spec §9). "claim" is a reserved slug (directory.services
    # .RESERVED_SLUGS) so no listing or category can shadow this.
    path("claim/<str:public_id>/", claim_views.claim_form, name="public-claim"),
    path(
        "claim/<str:public_id>/submitted/",
        claim_views.claim_submitted,
        name="public-claim-submitted",
    ),
    path(
        "claim/<str:public_id>/verify/",
        claim_views.claim_verify,
        name="public-claim-verify",
    ),
    path(
        "claim/<str:public_id>/verify/resend/",
        claim_views.claim_verify_resend,
        name="public-claim-resend",
    ),
    # Lead capture (spec §3.3). "lead" is a reserved slug.
    path("lead/<str:public_id>/", lead_views.lead_form, name="public-lead"),
    path("lead/<str:public_id>/sent/", lead_views.lead_sent, name="public-lead-sent"),
    # robots.txt and the sitemap. "sitemap.xml" / "sitemaps" are reserved
    # slugs; all three sit ahead of the catch-all.
    path("robots.txt", public_views.robots_txt, name="robots-txt"),
    path("sitemap.xml", public_views.sitemap_index, name="sitemap-index"),
    re_path(
        r"^sitemaps/(?P<kind>listings|categories)-(?P<shard>[1-9][0-9]*)\.xml$",
        public_views.sitemap_child,
        name="sitemap-child",
    ),
    path("", public_views.home, name="public-home"),
    re_path(r"^(?P<path>.+)$", public_views.public_dispatch, name="public-dispatch"),
]
