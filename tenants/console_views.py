"""The installation console: operator login, logout and the directory picker.

Served on ``OSDS_CONSOLE_HOST``. Every operator can sign in here even with no
membership anywhere -- accepting a pending invitation happens on the console
(decisions.md section 3). The session is host-only and does not carry to any
tenant's ``/admin``. Django admin stays mounted at ``/admin/`` for ``is_staff``
operators; this is the operator-facing surface alongside it.
"""

from __future__ import annotations

from django.contrib.auth import login as auth_login, logout as auth_logout
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from tenants.login import OperatorAuthenticationForm, safe_redirect_target
from tenants.models import StaffMembership


@never_cache
@require_http_methods(["GET", "POST"])
def login_view(request):
    fallback = reverse("console-index")
    if request.user.is_authenticated and request.method == "GET":
        return redirect(safe_redirect_target(request, fallback))
    form = OperatorAuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        auth_login(request, form.get_user())
        return redirect(safe_redirect_target(request, fallback))
    return render(
        request,
        "console/login.html",
        {"form": form, "next": request.POST.get("next") or request.GET.get("next") or ""},
    )


@never_cache
@require_http_methods(["POST"])
def logout_view(request):
    auth_logout(request)
    return redirect("console-login")


@never_cache
def index(request):
    if not request.user.is_authenticated:
        login_url = reverse("console-login")
        return redirect(f"{login_url}?next={request.get_full_path()}")

    # StaffMembership spans tenants by design; the console is where an operator
    # sees every directory they touch, with none in scope.
    memberships = list(
        StaffMembership.objects.filter(operator=request.user)
        .select_related("tenant")
        .order_by("tenant__name")
    )
    active = [m for m in memberships if m.status == StaffMembership.Status.ACTIVE]
    pending = [m for m in memberships if m.status == StaffMembership.Status.PENDING]
    return render(
        request, "console/index.html", {"active": active, "pending": pending}
    )
