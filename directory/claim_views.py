"""Public claim submission (spec §9, §9.0, §9.4). Served under
``/claim/<public_id>/`` on a tenant's own domain, ahead of the public-site
catch-all (``osds/urls_tenant.py``).

Verification mechanics, approval and anything writing ``Listing.status`` or
``Listing.owner`` are a later PR -- this view only ever collects the
submission and hands it to ``directory.services.submit_claim``.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from directory import services
from directory.forms import ClaimForm
from directory.masking import mask_email, mask_phone_e164
from directory.models import Claim, Listing


def _client_ip(request) -> str:
    return request.META.get("REMOTE_ADDR", "") or ""


@require_http_methods(["GET", "POST"])
def claim_form(request, public_id):
    listing = get_object_or_404(Listing.objects.published(), public_id=public_id)
    tenant = request.tenant
    consent_text = services.get_default_consent_text(tenant)
    enabled_methods = services.enabled_claim_methods(tenant)

    if request.method == "POST":
        form = ClaimForm(request.POST, enabled_methods=enabled_methods)
        if form.is_valid():
            try:
                claim = services.submit_claim(
                    tenant,
                    listing=listing,
                    method=form.cleaned_data["method"],
                    claimant={
                        "name": form.cleaned_data["name"],
                        "email": form.cleaned_data["email"],
                        "phone_e164": form.cleaned_data["phone_e164"],
                        "role_claimed": form.cleaned_data["role_claimed"],
                    },
                    consent=form.consent_payload(),
                    ip=_client_ip(request),
                )
            except services.ConsentRequired:
                form.add_error(None, "Please respond to every consent option below.")
            except services.SchemaError as exc:
                form.add_error(None, "; ".join(exc.errors))
            else:
                return redirect("public-claim-submitted", public_id=claim.public_id)
    else:
        form = ClaimForm(
            enabled_methods=enabled_methods,
            initial={"role_claimed": "owner"},
        )

    return render(
        request,
        "public/claim_form.html",
        {
            "listing": listing,
            "form": form,
            "consent_text": consent_text,
            "masked_phone": mask_phone_e164(listing.phone_e164)
            if listing.phone_e164
            else "",
            "masked_email": mask_email(listing.email) if listing.email else "",
        },
    )


@require_http_methods(["GET"])
def claim_submitted(request, public_id):
    claim = get_object_or_404(Claim.objects, public_id=public_id)
    return render(request, "public/claim_submitted.html", {"claim": claim})
