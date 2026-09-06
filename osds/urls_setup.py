"""URLconf served on every host while first-run setup is incomplete.

``TenantResolutionMiddleware`` routes here until ``InstallSetup.completed_at``
is set; after that these views 404 and the middleware stops routing here.
"""

from django.urls import path, re_path
from django.views.generic.base import RedirectView

from tenants.wizard import views

urlpatterns = [
    path("setup/", views.index, name="setup-index"),
    path("setup/unlock/", views.unlock, name="setup-unlock"),
    path("setup/account/", views.account, name="setup-account"),
    path("setup/directory/", views.directory, name="setup-directory"),
    path("setup/domain/", views.domain, name="setup-domain"),
    path("setup/storage/", views.storage, name="setup-storage"),
    path("setup/smtp/", views.smtp, name="setup-smtp"),
    path("setup/claims/", views.claims, name="setup-claims"),
    path("setup/done/", views.done, name="setup-done"),
    # Anything else on any host goes to the wizard.
    re_path(r"^(?!setup/).*$", RedirectView.as_view(url="/setup/", permanent=False)),
]
