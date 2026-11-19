"""Shared pieces for the operator login form.

The tenant-admin host (/admin/login/) and the console host (/login/) run the
same authentication -- an ``Operator`` (AUTH_USER_MODEL) proving email plus
password. They differ only in URL names and templates. Signing in grants
nothing on its own: every real admin view is still gated by an active
``StaffMembership`` (spec section 4.4, directory.access).
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm
from django.utils.http import url_has_allowed_host_and_scheme


class OperatorAuthenticationForm(AuthenticationForm):
    """``AuthenticationForm`` that lowercases the submitted email.

    ``Operator.email`` is stored lowercased (tenants.models.OperatorManager)
    and ``ModelBackend`` matches it exactly, so a mixed-case entry would
    otherwise fail to authenticate a valid account.
    """

    def clean_username(self) -> str:
        return (self.cleaned_data.get("username") or "").strip().lower()


class OperatorAdminAuthenticationForm(AdminAuthenticationForm):
    """``AdminAuthenticationForm`` that lowercases the submitted email.

    Django's admin login passes the raw input to ``get_by_natural_key``, so a
    mixed-case entry is rejected at ``/admin/login/`` while the same
    credentials succeed at ``OperatorAuthenticationForm`` (#143).
    """

    def clean_username(self) -> str:
        return (self.cleaned_data.get("username") or "").strip().lower()


def safe_redirect_target(request, fallback: str) -> str:
    """A caller-supplied ``next``, but only if it stays on this host.

    Guards the open-redirect that a bare ``?next=`` otherwise is. A relative
    path passes; an absolute URL to any other host falls back.
    """
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return fallback


INSECURE_COOKIE_WARNING = (
    "This page was reached over plain HTTP, but the session cookies are marked "
    "Secure. A browser discards a Secure cookie set from an HTTP page, so "
    "signing in cannot stick and you will be sent back here. Serve this site "
    "over HTTPS, or, for an HTTP-only install, set OSDS_SECURE_COOKIES=false "
    "and restart."
)


def insecure_cookie_warning(request) -> str:
    """The diagnostic for a Secure-cookie install reached over plain http.

    Empty when the request is https (directly or via a trusted proxy header,
    which ``request.is_secure()`` already honours) or when neither cookie is
    marked Secure. Shown on every render of a login form, so an operator whose
    correct credentials bounce back to the form is told why (#139).
    """
    if request.is_secure():
        return ""
    if settings.SESSION_COOKIE_SECURE or settings.CSRF_COOKIE_SECURE:
        return INSECURE_COOKIE_WARNING
    return ""
