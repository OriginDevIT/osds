"""The installation console: operator login, logout and the directory picker.

Served on ``OSDS_CONSOLE_HOST``. Every operator can sign in here even with no
membership anywhere -- accepting a pending invitation happens on the console
(decisions.md section 3). The session is host-only and does not carry to any
tenant's ``/admin``. Django admin stays mounted at ``/admin/`` for ``is_staff``
operators; this is the operator-facing surface alongside it.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.password_validation import password_validators_help_texts
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from tenants import operator_invites
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


class SetPasswordForm(forms.Form):
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The passwords do not match.")
        return cleaned


def _private(response):
    """No cross-origin referrer and no caching on a page whose URL is a
    credential. ``same-origin``, not ``no-referrer``, for the reason in
    ``directory.owner_views._private``: ``no-referrer`` makes a browser send
    ``Origin: null`` on the page's own form POST and CSRF refuses it."""
    response["Referrer-Policy"] = "same-origin"
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@require_http_methods(["GET", "POST"])
def invite_view(request, token):
    """The operator's set-password page (decisions.md section 4.14). GET shows
    the form and spends nothing, so a mail scanner that follows the link leaves
    it live; POST spends it. Unknown, expired, used and no-longer-needed links
    are the same page, 400. No session is opened: the operator signs in."""
    if request.method == "POST":
        form = SetPasswordForm(request.POST)
        if form.is_valid():
            try:
                operator_invites.set_password(
                    secret=token, password=form.cleaned_data["password1"]
                )
            except operator_invites.InviteRefused:
                return _private(render(request, "console/invite_invalid.html", status=400))
            except operator_invites.PasswordRejected as exc:
                for message in exc.messages:
                    form.add_error("password1", message)
            else:
                messages.success(request, "Password set. Sign in to continue.")
                return _private(redirect("console-login"))
    else:
        if operator_invites.peek_invite(token) is None:
            return _private(render(request, "console/invite_invalid.html", status=400))
        form = SetPasswordForm()
    return _private(
        render(
            request,
            "console/invite.html",
            {"form": form, "hints": password_validators_help_texts()},
        )
    )
