"""URLconf served on the installation console host (``OSDS_CONSOLE_HOST``).

Two surfaces share the host. ``console_views`` is the operator-facing console:
sign in, sign out, and a directory picker that every operator reaches --
accepting a pending invitation happens here (decisions.md section 3). Django
admin stays mounted at ``/admin/`` for the installation console proper, which
is ``is_staff``-only (decisions.md section 4).
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
