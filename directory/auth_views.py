"""Operator login, logout and the tenant-admin landing page.

Served under ``/admin/`` on a tenant's own domain. The session set here is
host-only (``SESSION_COOKIE_DOMAIN`` is None) so it never carries to the
console or to another tenant's ``/admin``. Authorization for every real admin
view is still the active ``StaffMembership`` check in ``directory.access`` --
signing in grants nothing (spec section 4.4).
"""

from __future__ import annotations

from django.contrib.auth import login as auth_login, logout as auth_logout
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from tenants.login import OperatorAuthenticationForm, safe_redirect_target
from tenants.models import StaffMembership


def _fallback(request) -> str:
    return reverse("directory_admin:index")


@never_cache
@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated and request.method == "GET":
        return redirect(safe_redirect_target(request, _fallback(request)))
    form = OperatorAuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        auth_login(request, form.get_user())
        return redirect(safe_redirect_target(request, _fallback(request)))
    return render(
        request,
        "directory/admin/login.html",
        {"form": form, "next": request.POST.get("next") or request.GET.get("next") or ""},
    )


@never_cache
@require_http_methods(["POST"])
def logout_view(request):
    auth_logout(request)
    return redirect("directory_admin:login")


@never_cache
def index(request):
    """The tenant-admin landing.

    Renders from ``request.tenant`` alone. An operator with no active
    membership on *this* tenant sees the same page whether or not they
    administer other directories -- a response that varied with other-tenant
    membership would be an oracle for it (decisions.md section 3). A pending
    membership on this tenant is named; that is local information the operator
    already holds.
    """
    if not request.user.is_authenticated:
        login_url = reverse("directory_admin:login")
        return redirect(f"{login_url}?next={request.get_full_path()}")

    tenant = request.tenant
    membership = StaffMembership.objects.filter(
        operator=request.user,
        tenant=tenant,
        status=StaffMembership.Status.ACTIVE,
    ).first()
    pending = StaffMembership.objects.filter(
        operator=request.user,
        tenant=tenant,
        status=StaffMembership.Status.PENDING,
    ).exists()
    return render(
        request,
        "directory/admin/index.html",
        {"tenant": tenant, "membership": membership, "pending": pending},
    )
