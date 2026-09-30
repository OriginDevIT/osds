"""The tenant-admin mail settings page (decisions.md §4.5).

Served at ``/admin/settings/mail/``. A credentials surface, so the top tenant
role only. Writes go through ``tenants.services.update_mail_settings``; the
password is never rendered back.
"""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from directory.access import tenant_admin_required
from tenants import services
from tenants.mail_forms import MailSettingsForm
from tenants.models import StaffMembership


@never_cache
@tenant_admin_required(StaffMembership.Role.ADMIN)
@require_http_methods(["GET", "POST"])
def mail_settings(request):
    tenant = request.tenant
    form = MailSettingsForm(request.POST or None, tenant=tenant)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_mail_settings(
                tenant=tenant,
                config=form.config(),
                password=form.cleaned_data["password"],
                clear_password=form.cleaned_data["clear_password"],
                changed_by=request.user,
            )
        except services.InvalidTenantSettings as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Mail settings saved.")
            return redirect("directory_admin:mail-settings")
    return render(request, "directory/admin/mail_settings.html", {"form": form})
