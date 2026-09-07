"""Shared pieces for the operator login form.

The tenant-admin host (/admin/login/) and the console host (/login/) run the
same authentication -- an ``Operator`` (AUTH_USER_MODEL) proving email plus
password. They differ only in URL names and templates. Signing in grants
nothing on its own: every real admin view is still gated by an active
``StaffMembership`` (spec section 4.4, directory.access).
"""

from __future__ import annotations

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
