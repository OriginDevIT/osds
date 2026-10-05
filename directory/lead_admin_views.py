"""The tenant-admin lead pages (decisions.md §4.10).

* ``/admin/leads/`` and a lead's page: **moderator** and above. A moderator sees
  the message -- they cannot judge spam without it -- and contact details masked.
  A **manager** sees them in full (spec §4.4: the cut between 3 and 2 is money
  and personal data). Every detail view writes an access-log row, masked or not.
* ``lead.mark_spam``: moderator.
* ``/admin/settings/leads/``: admin. The switch is off until it is turned on.

Support (rank 0) sees none of it. Each POST route calls exactly one command in
``directory.leads``, which checks the role again: the decorator is the page
gate, the service is the authority.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from audit.access_log import record_view
from directory import leads
from directory.access import tenant_admin_required
from directory.masking import mask_email, mask_phone_e164
from directory.models import Consent, Lead
from osds.client_ip import client_ip
from tenants import services as tenant_services
from tenants.models import StaffMembership

_VIEW = tenant_admin_required(StaffMembership.Role.MODERATOR)
_ADMIN = tenant_admin_required(StaffMembership.Role.ADMIN)
FULL_ROLE = StaffMembership.Role.MANAGER
PAGE_SIZE = 50
TABS = ("inbox", "spam")


@never_cache
@_VIEW
def leads_list(request):
    tab = request.GET.get("tab")
    if tab not in TABS:
        tab = "inbox"
    base = Lead.objects.select_related("listing")
    page = Paginator(
        base.filter(marked_spam=(tab == "spam")).order_by("-created_at", "-id"), PAGE_SIZE
    ).get_page(request.GET.get("page"))
    for lead in page:
        lead.masked_email = mask_email(lead.email)
    return render(
        request,
        "directory/admin/leads_list.html",
        {
            "tab": tab,
            "page": page,
            "counts": {
                "inbox": base.filter(marked_spam=False).count(),
                "spam": base.filter(marked_spam=True).count(),
            },
        },
    )


@never_cache
@_VIEW
def lead_detail(request, public_id):
    lead = get_object_or_404(Lead.objects.select_related("listing"), public_id=public_id)
    full = request.membership.role >= FULL_ROLE
    record_view(
        tenant=request.tenant,
        operator=request.user,
        role=request.membership.role,
        resource_type="lead",
        resource_id=lead.public_id,
        ip=client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        extra={"masked": not full},
    )
    return render(
        request,
        "directory/admin/lead_detail.html",
        {
            "lead": lead,
            "listing": lead.listing,
            "full": full,
            "email": lead.email if full else mask_email(lead.email),
            "phone": (
                lead.phone_e164 if full else (mask_phone_e164(lead.phone_e164) if lead.phone_e164 else "")
            ),
            "consents": Consent.objects.filter(lead=lead).order_by("id") if full else [],
        },
    )


@_VIEW
@require_POST
def lead_mark_spam(request, public_id):
    lead = get_object_or_404(Lead.objects, public_id=public_id)
    try:
        leads.mark_lead_spam(request.tenant, lead=lead, operator=request.user)
    except leads.LeadRefused as exc:
        messages.error(request, leads.REFUSAL_TEXT.get(exc.reason, "That could not be done."))
    else:
        messages.success(request, "Marked as spam.")
    return redirect("directory_admin:lead-detail", public_id=public_id)


class LeadSettingsForm(forms.Form):
    enabled = forms.BooleanField(
        required=False,
        label="Show an inquiry form on published listings",
    )


@never_cache
@_ADMIN
@require_http_methods(["GET", "POST"])
def lead_settings(request):
    tenant = request.tenant
    # Bound on the method, not on a non-empty POST: an unchecked box with no
    # other field posts nothing, and that is exactly "turn it off".
    form = LeadSettingsForm(
        request.POST if request.method == "POST" else None,
        initial={"enabled": leads.leads_enabled(tenant)},
    )
    if request.method == "POST" and form.is_valid():
        try:
            tenant_services.save_settings_page(
                tenant, page="leads", block="leads",
                value={"enabled": form.cleaned_data["enabled"]}, changed_by=request.user,
            )
        except tenant_services.InvalidTenantSettings as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Lead form settings saved.")
            return redirect("directory_admin:lead-settings")
    return render(
        request,
        "directory/admin/lead_settings.html",
        {"form": form, "consent_text": leads.get_lead_consent_text(tenant)},
    )
