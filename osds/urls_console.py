"""URLconf served on the installation console host (``OSDS_CONSOLE_HOST``).

Django admin is used for the installation console only (decisions.md §4). The
directory picker and invitation acceptance land here in a later PR.
"""

from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
]
