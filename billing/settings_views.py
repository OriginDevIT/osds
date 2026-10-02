"""The generic payments settings page, ``/admin/settings/payments/``
(decisions.md §4.11). A credentials surface, so the admin only.

The form is built from the registered provider's field list: core renders what
the provider declares and never names the provider. Secrets are never rendered
back; blank keeps the stored one.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from billing import payments, settings_service
from directory import routing
from directory.access import tenant_admin_required
from osds.adapter_context import adapter_config
from tenants.models import StaffMembership


def _form_class(provider):
    attrs: dict = {}
    for f in provider.settings_fields():
        if f.secret:
            attrs[f.key] = forms.CharField(
                label=f.label, required=False, help_text=f.help_text,
                widget=forms.PasswordInput(render_value=False),
            )
            attrs[f"clear_{f.key}"] = forms.BooleanField(
                label=f"Remove the stored {f.label.lower()}", required=False
            )
        else:
            attrs[f.key] = forms.CharField(label=f.label, required=False, help_text=f.help_text)
    return type("PaymentSettingsForm", (forms.Form,), attrs)


@never_cache
@tenant_admin_required(StaffMembership.Role.ADMIN)
@require_http_methods(["GET", "POST"])
def payment_settings(request):
    tenant = request.tenant
    provider = payments.provider()
    if provider is None:
        return render(request, "billing/admin/payment_settings.html", {"provider": None})

    fields = list(provider.settings_fields())
    form_class = _form_class(provider)
    config = adapter_config(tenant, provider.adapter_id)
    stored = settings_service.stored_secrets(tenant, provider)
    initial = {f.key: config.get(f.key, "") for f in fields if not f.secret}
    form = form_class(request.POST or None, initial=initial)

    if request.method == "POST" and form.is_valid():
        values = {f.key: form.cleaned_data.get(f.key, "") for f in fields}
        clear = {f.key for f in fields if f.secret and form.cleaned_data.get(f"clear_{f.key}")}
        try:
            settings_service.update_adapter_settings(
                tenant, provider=provider, values=values, clear=clear, changed_by=request.user
            )
        except settings_service.AdapterSettingsError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Payment settings saved.")
            return redirect("billing_admin:payment-settings")

    for f in fields:
        if f.secret and f.key in stored:
            form.fields[f.key].help_text = (f.help_text + " A value is stored; leave blank to keep it.").strip()

    webhook = (
        routing.absolute_url(tenant, f"/_adapters/{provider.adapter_id}/inbound/")
        if routing.has_absolute_base(tenant) else ""
    )
    return render(
        request,
        "billing/admin/payment_settings.html",
        {
            "provider": provider,
            "form": form,
            "webhook_url": webhook,
            "available": payments.available(tenant),
            "has_base": routing.has_absolute_base(tenant),
        },
    )
