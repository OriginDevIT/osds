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

from billing import inbound as billing_inbound
from billing import owner_views as billing_owner_views
from directory import claim_views, owner_edit_views, owner_views, public_views

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
    path("admin/", include("billing.admin_urls")),
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
    # Owner sign-in and dashboard (decisions.md §4.9). "owner" is a reserved
    # slug (directory.services.RESERVED_SLUGS), so nothing public shadows it.
    path("owner/", owner_views.dashboard, name="owner-dashboard"),
    path("owner/signin/", owner_views.signin_request, name="owner-signin"),
    path("owner/signin/sent/", owner_views.signin_sent, name="owner-signin-sent"),
    path(
        "owner/signin/<str:token>/",
        owner_views.signin_confirm,
        name="owner-signin-confirm",
    ),
    path("owner/signout/", owner_views.signout, name="owner-signout"),
    path(
        "owner/listings/<str:public_id>/",
        owner_edit_views.listing_manage,
        name="owner-listing",
    ),
    path(
        "owner/listings/<str:public_id>/media/add/",
        owner_edit_views.media_add,
        name="owner-media-add",
    ),
    path(
        "owner/listings/<str:public_id>/media/<str:asset_public_id>/remove/",
        owner_edit_views.media_remove,
        name="owner-media-remove",
    ),
    path(
        "owner/listings/<str:public_id>/billing/checkout/",
        billing_owner_views.checkout,
        name="owner-billing-checkout",
    ),
    path(
        "owner/listings/<str:public_id>/billing/cancel/",
        billing_owner_views.cancel,
        name="owner-billing-cancel",
    ),
    path(
        "owner/listings/<str:public_id>/billing/portal/",
        billing_owner_views.portal,
        name="owner-billing-portal",
    ),
    path(
        "owner/listings/<str:public_id>/billing/return/",
        billing_owner_views.return_page,
        name="owner-billing-return",
    ),
    # A payment provider's webhook (decisions.md §4.11). CSRF-exempt: the
    # caller is a provider's server, and the adapter verifies its signature.
    path(
        "_adapters/<slug:adapter_id>/inbound/",
        billing_inbound.adapter_inbound,
        name="adapter-inbound",
    ),
    # A placeholder until lead capture ships: 404, and nothing links to it.
    path("owner/leads/", owner_edit_views.leads, name="owner-leads"),
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
