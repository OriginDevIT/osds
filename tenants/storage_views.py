"""The tenant-admin Storage settings page, served at ``/admin/settings/storage/``.

A credentials surface, so the top tenant role only. The form probes a cloud
backend before it validates; the save goes through ``save_settings_page`` so the
command log names fields and never values.
"""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from directory.access import tenant_admin_required
from tenants import services
from tenants.models import StaffMembership
from tenants.storage_forms import StorageForm


@never_cache
@tenant_admin_required(StaffMembership.Role.ADMIN)
@require_http_methods(["GET", "POST"])
def storage_settings(request):
    tenant = request.tenant
    form = StorageForm(request.POST or None, tenant=tenant)
    if request.method == "POST" and form.is_valid():
        try:
            services.save_settings_page(
                tenant, page="storage", block="storage", value=form.config(),
                changed_by=request.user, secrets=form.new_secrets(),
            )
        except services.InvalidTenantSettings as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Storage settings saved.")
            return redirect("directory_admin:storage-settings")
    return render(request, "directory/admin/storage_settings.html", {"form": form})
