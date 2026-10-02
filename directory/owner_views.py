"""Owner sign-in and the owner dashboard (decisions.md §4.9).

Served under ``/owner/`` on a tenant's own domain, ahead of the public-site
catch-all (``osds.urls_tenant``; ``owner`` is a reserved slug). Nothing here
touches ``request.user``: an owner's session is its own (``directory.
owner_auth``), and an operator's never satisfies it.

Editing a listing, its media and the leads placeholder live in
``directory.owner_edit_views``.
"""

from __future__ import annotations

from django import forms
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from audit.ratelimit import RateLimited
from directory import owner_auth
from directory.claim_views import _limit_message, _limited
from directory.models import Listing
from directory.owner_auth import owner_required
from directory.owner_edit_views import owned_leads
from osds.client_ip import client_ip as _client_ip


class SignInRequestForm(forms.Form):
    email = forms.EmailField(label="Your email address")


def _private(response):
    """No cross-origin referrer and no caching on a page whose URL is a
    credential. ``same-origin``, not ``no-referrer``: with ``no-referrer`` a
    browser sends ``Origin: null`` on the confirm page's own form POST, and
    Django's CSRF origin check refuses it. ``same-origin`` still sends nothing
    to any other site, so the token URL never leaves this one."""
    response["Referrer-Policy"] = "same-origin"
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@require_http_methods(["GET", "POST"])
def signin_request(request):
    tenant = request.tenant
    if not owner_auth.sign_in_available(tenant):
        return render(request, "owner/signin_unavailable.html")
    form = SignInRequestForm(request.POST or None)
    limited = None
    if request.method == "POST" and form.is_valid():
        try:
            outcome = owner_auth.request_sign_in(
                tenant, email=form.cleaned_data["email"], ip=_client_ip(request)
            )
        except RateLimited as exc:
            limited = exc
            form.add_error(None, _limit_message(exc))
        else:
            if outcome == "unavailable":
                return render(request, "owner/signin_unavailable.html")
            # "sent" and "ignored" are the same page, deliberately.
            return redirect("owner-signin-sent")
    response = render(request, "owner/signin.html", {"form": form})
    return _limited(response, limited) if limited else response


@never_cache
def signin_sent(request):
    return render(request, "owner/signin_sent.html")


@never_cache
@require_http_methods(["GET", "POST"])
def signin_confirm(request, token):
    """GET shows a button and spends nothing -- a mail scanner that follows the
    link leaves it live. POST spends it."""
    tenant = request.tenant
    if request.method == "POST":
        try:
            new = owner_auth.sign_in(tenant, secret=token)
        except owner_auth.SignInRefused:
            return _private(render(request, "owner/signin_invalid.html", status=400))
        response = redirect("owner-dashboard")
        response.set_cookie(owner_auth.COOKIE_NAME, new.secret, **owner_auth.cookie_kwargs())
        return _private(response)
    if owner_auth.peek_token(tenant, token) is None:
        return _private(render(request, "owner/signin_invalid.html", status=400))
    return _private(render(request, "owner/signin_confirm.html"))


def _owned(request):
    return (
        Listing.objects.filter(owner=request.owner)
        .select_related("listing_type", "current_tier")
        .order_by("name", "id")
    )


@never_cache
@owner_required
def dashboard(request):
    return render(
        request,
        "owner/dashboard.html",
        {"listings": _owned(request), "lead_count": owned_leads(request.owner).count()},
    )


@owner_required
@require_POST
def signout(request):
    owner_auth.sign_out(
        request.tenant,
        session=request.owner_session,
        everywhere=request.POST.get("everywhere") == "1",
    )
    response = redirect("owner-signin")
    response.delete_cookie(owner_auth.COOKIE_NAME, path=owner_auth.COOKIE_PATH)
    return response
