"""The adapter pages of the tenant admin (decisions.md §4.12): the list, one
adapter's settings, its deliveries and dead letters, and replay.

Generic: every page is built from what the registered subscriber declares
(``settings_fields()``, ``scopes``, ``rotatable_secrets``, ``send_test``), so
core never imports or names an adapter. All of it is admin only: it holds
credentials, grants access to personal data, and can re-send events.

A secret is shown exactly once, in the response to the request that created it,
and is never stored where this page could show it again.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.conf import settings as django_settings
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from audit import deliveries
from audit.models import OutboxDelivery
from directory.access import tenant_admin_required
from osds.adapter_context import adapter_config, build_context, provider_id, scope_setting
from osds.adapters import all_subscribers, subscriber_by_id
from tenants import adapter_settings as svc
from tenants.models import StaffMembership

_ADMIN = tenant_admin_required(StaffMembership.Role.ADMIN)


def _subscriber_or_404(adapter_id):
    subscriber = subscriber_by_id(adapter_id)
    if subscriber is None or not hasattr(subscriber, "settings_fields"):
        raise Http404
    return subscriber


def _form_class(subscriber):
    attrs: dict = {}
    for field in svc.settings_fields(subscriber):
        if field.secret:
            attrs[field.key] = forms.CharField(
                label=field.label, required=False, help_text=field.help_text,
                widget=forms.PasswordInput(render_value=False),
            )
            attrs[f"clear_{field.key}"] = forms.BooleanField(
                label=f"Remove the stored {field.label.lower()}", required=False
            )
        elif field.kind == "bool":
            attrs[field.key] = forms.BooleanField(label=field.label, required=False, help_text=field.help_text)
        else:
            attrs[field.key] = forms.CharField(label=field.label, required=False, help_text=field.help_text)
    for _, name, label in svc.scope_labels(subscriber):
        attrs[name] = forms.BooleanField(label=label, required=False)
    return type("AdapterSettingsForm", (forms.Form,), attrs)


@never_cache
@_ADMIN
def adapter_list(request):
    rows = []
    for subscriber in all_subscribers():
        if not hasattr(subscriber, "settings_fields"):
            continue
        config = adapter_config(
            request.tenant, provider_id(subscriber), config_key=getattr(subscriber, "config_key", None)
        )
        rows.append({
            "id": provider_id(subscriber),
            "enabled": config.get("enabled") is True,
            "health": deliveries.health(request.tenant, provider_id(subscriber)),
        })
    return render(request, "directory/admin/adapter_list.html", {"rows": rows})


@never_cache
@_ADMIN
@require_http_methods(["GET", "POST"])
def adapter_settings(request, adapter_id):
    tenant = request.tenant
    subscriber = _subscriber_or_404(adapter_id)
    fields = svc.settings_fields(subscriber)
    scopes = svc.scope_labels(subscriber)
    config = adapter_config(tenant, adapter_id, config_key=getattr(subscriber, "config_key", None))
    initial = {f.key: config.get(f.key, False if f.kind == "bool" else "") for f in fields if not f.secret}
    initial.update({name: config.get(scope_setting(scope)) is True for scope, name, _ in scopes})
    form_class = _form_class(subscriber)
    action = request.POST.get("action", "save") if request.method == "POST" else ""
    form = form_class(request.POST if action == "save" else None, initial=initial)
    shown_once: dict = {}
    test_result = None

    if request.method == "POST" and action == "save" and form.is_valid():
        data = form.cleaned_data
        try:
            shown_once = svc.update_adapter_settings(
                tenant,
                subscriber=subscriber,
                values={f.key: data.get(f.key) for f in fields},
                grants={scope: bool(data.get(name)) for scope, name, _ in scopes},
                clear={f.key for f in fields if f.secret and data.get(f"clear_{f.key}")},
                changed_by=request.user,
            )
        except svc.AdapterSettingsError as exc:
            form.add_error(None, str(exc))
        else:
            if not shown_once:
                messages.success(request, "Settings saved.")
                return redirect("directory_admin:adapter-settings", adapter_id=adapter_id)
            tenant.refresh_from_db()
            config = adapter_config(tenant, adapter_id, config_key=getattr(subscriber, "config_key", None))
            form = form_class(initial=initial)
    elif request.method == "POST" and action == "rotate":
        try:
            key = request.POST.get("key", "")
            shown_once = {key: svc.rotate_secret(tenant, subscriber=subscriber, key=key, operator=request.user)}
        except svc.RotateRefused:
            messages.error(request, "That secret cannot be rotated.")
            return redirect("directory_admin:adapter-settings", adapter_id=adapter_id)
    elif request.method == "POST" and action == "test" and hasattr(subscriber, "send_test"):
        ok, detail = subscriber.send_test(build_context(tenant, subscriber))
        test_result = {"ok": ok, "detail": detail}

    stored = svc.stored_secrets(tenant, subscriber)
    for field in fields:
        if field.secret and field.key in stored and field.key in form.fields:
            form.fields[field.key].help_text = (field.help_text + " A value is stored; leave blank to keep it.").strip()
    return render(
        request,
        "directory/admin/adapter_settings.html",
        {
            "adapter_id": adapter_id,
            "form": form,
            "scopes": [(name, label) for _, name, label in scopes],
            "stored": stored,
            "rotatable": svc.rotatable(subscriber),
            "can_test": hasattr(subscriber, "send_test"),
            "shown_once": shown_once,
            "test_result": test_result,
            "host": config.get("host", ""),
            "health": deliveries.health(tenant, adapter_id),
            "allow_private": django_settings.OSDS_WEBHOOK_ALLOW_PRIVATE,
        },
    )


@never_cache
@_ADMIN
def adapter_deliveries(request, adapter_id):
    _subscriber_or_404(adapter_id)
    tab = request.GET.get("tab")
    if tab not in ("recent", "dead"):
        tab = "recent"
    rows = deliveries.recent(
        request.tenant, adapter_id, status=OutboxDelivery.Status.DEAD if tab == "dead" else None
    )
    return render(
        request,
        "directory/admin/adapter_deliveries.html",
        {
            "adapter_id": adapter_id,
            "tab": tab,
            "rows": rows,
            "health": deliveries.health(request.tenant, adapter_id),
        },
    )


@_ADMIN
@require_POST
def adapter_replay(request, adapter_id, event_id):
    _subscriber_or_404(adapter_id)
    try:
        deliveries.replay_delivery(
            request.tenant, adapter_id=adapter_id, event_id=event_id, operator=request.user
        )
    except deliveries.ReplayRefused as exc:
        messages.error(request, deliveries.REFUSAL_TEXT.get(exc.reason, "That could not be replayed."))
    else:
        messages.success(request, "Queued to send again.")
    return redirect(reverse("directory_admin:adapter-deliveries", args=[adapter_id]) + "?tab=dead")
