"""Access control for the tenant admin.

Enforced by an active ``StaffMembership`` on the resolved tenant, at or above
a required role rank -- not by prompt (spec §4.4, CLAUDE.md invariant 10). The
operator login form is a separate PR; tests use ``Client.force_login``.
"""

from __future__ import annotations

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.http import Http404, HttpResponseForbidden
from django.urls import reverse

from tenants.models import StaffMembership


def tenant_admin_required(min_role: int = StaffMembership.Role.ADMIN):
    """Require an authenticated operator with an active membership on
    ``request.tenant`` of at least ``min_role``. Anonymous -> redirect to the
    login form. Authenticated but no membership -> 404 (the admin surface is
    not discoverable, and the response must not vary with who is asking); too
    low a rank -> 403.
    """

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            tenant = getattr(request, "tenant", None)
            if getattr(request, "osds_host_kind", None) != "tenant" or tenant is None:
                raise Http404()
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(
                    request.get_full_path(),
                    login_url=reverse("directory_admin:login"),
                )
            membership = StaffMembership.objects.filter(
                operator=user,
                tenant=tenant,
                status=StaffMembership.Status.ACTIVE,
            ).first()
            if membership is None:
                raise Http404()
            if membership.role < min_role:
                return HttpResponseForbidden(
                    "Your role does not have access to this section."
                )
            request.membership = membership
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
