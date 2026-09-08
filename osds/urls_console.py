"""URLconf served on the installation console host (``OSDS_CONSOLE_HOST``).

Two surfaces share the host. ``console_views`` is the operator-facing console:
sign in, sign out, and a directory picker that every operator reaches --
accepting a pending invitation happens here (decisions.md section 3). Django
admin stays mounted at ``/admin/`` for the installation console proper. The
``AdminSite`` login gate is ``is_staff``, but the registered models --
``Tenant``, ``Operator`` and ``StaffMembership`` -- gate every permission hook
on ``is_superadmin``: each is an installation-scoped act that no membership can
authorise (spec section 4.4, #146).
"""

from django.contrib import admin
from django.urls import path

from tenants import console_views

urlpatterns = [
    path("", console_views.index, name="console-index"),
    path("login/", console_views.login_view, name="console-login"),
    path("logout/", console_views.logout_view, name="console-logout"),
    path("admin/", admin.site.urls),
]
