"""The public lead form (spec §3.3 ``lead.*``; decisions.md §4.10).

Served under ``/lead/<listing public_id>/`` on a tenant's own domain, ahead of
the public-site catch-all. 404 unless the tenant has turned lead forms on and
the listing is published, so a disabled directory exposes nothing.

Two traps catch most bots before they cost a command: a hidden field no person
fills, and a signed render time no person beats. A trapped submission gets the
ordinary confirmation and creates nothing. The page is never cached and never
indexed.
"""

from __future__ import annotations

from django import forms
from django.core import signing
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from audit.ratelimit import RateLimited
from directory import leads, limit_responses, routing
from directory.models import Listing, ListingType
from osds.client_ip import client_ip

_SIGNER = signing.Signer(salt="osds.lead-form")
MIN_FILL_SECONDS = 3
MAX_FORM_AGE_SECONDS = 2 * 60 * 60


class LeadForm(forms.Form):
    name = forms.CharField(max_length=leads.NAME_MAX)
    email = forms.EmailField()
    phone_e164 = forms.CharField(max_length=16, required=False, label="Phone (optional)")
    message = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 6}),
        min_length=leads.MESSAGE_MIN,
        max_length=leads.MESSAGE_MAX,
    )
    # Unchecked is an explicit decline, not an omission (spec §9.0); the command
    # rejects it, so the refusal is logged.
    contact_by_business = forms.BooleanField(required=False)
    # The traps. Neither is validated here; the view reads them from the raw POST.
    company_url = forms.CharField(required=False, label="Leave this empty")
    form_token = forms.CharField(required=False, widget=forms.HiddenInput)

    def consent_payload(self) -> dict:
        return {
            leads.LEAD_CONSENT_CHANNEL: {
                "granted": bool(self.cleaned_data.get("contact_by_business"))
            }
        }


def issue_token(now) -> str:
    return _SIGNER.sign(str(int(now.timestamp())))


def seconds_since_issued(token: str, now) -> "int | None":
    """Seconds since ``token`` was issued, or ``None`` if it is missing,
    forged, or older than ``MAX_FORM_AGE_SECONDS``."""
    try:
        issued = int(_SIGNER.unsign(token or ""))
    except (signing.BadSignature, ValueError):
        return None
    elapsed = int(now.timestamp()) - issued
    return elapsed if 0 <= elapsed <= MAX_FORM_AGE_SECONDS else None


def _noindex(response):
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _listing_or_404(request, public_id) -> Listing:
    if not leads.leads_enabled(request.tenant):
        raise Http404
    listing = get_object_or_404(
        Listing.objects.published().select_related("listing_type"), public_id=public_id
    )
    if listing.status == Listing.Status.SUSPENDED:
        raise Http404
    return listing


def _source_page(listing) -> str:
    """Where the inquiry came from, worked out here from the listing -- a value
    the visitor sends would be theirs to set."""
    category = routing.canonical_category(listing)
    if category is None:
        return ""
    multi = ListingType.objects.count() > 1
    return routing.listing_url(listing.listing_type, category, listing, multi=multi) or ""


@never_cache
@require_http_methods(["GET", "POST"])
def lead_form(request, public_id):
    tenant = request.tenant
    listing = _listing_or_404(request, public_id)
    now = timezone.now()
    limited = None

    if request.method == "POST":
        form = LeadForm(request.POST)
        elapsed = seconds_since_issued(request.POST.get("form_token", ""), now)
        trapped = bool(request.POST.get("company_url")) or (
            elapsed is not None and elapsed < MIN_FILL_SECONDS
        )
        if trapped:
            try:
                leads.reject_spam_trap(tenant, listing=listing, ip=client_ip(request))
            except RateLimited as exc:
                limited = exc
                form.is_valid()  # runs the validation add_error needs
                form.add_error(None, limit_responses.limit_message(exc))
            else:
                return redirect("public-lead-sent", public_id=listing.public_id)
        elif form.is_valid():
            if elapsed is None:
                form.add_error(None, "This form has expired. Please check it and send it again.")
            else:
                try:
                    leads.create_lead(
                        tenant,
                        listing=listing,
                        kind="contact_form",
                        contact={
                            "name": form.cleaned_data["name"],
                            "email": form.cleaned_data["email"],
                            "phone_e164": form.cleaned_data["phone_e164"],
                        },
                        message=form.cleaned_data["message"],
                        consent=form.consent_payload(),
                        source_page=_source_page(listing),
                        ip=client_ip(request),
                    )
                except RateLimited as exc:
                    limited = exc
                    form.add_error(None, limit_responses.limit_message(exc))
                except leads.ConsentRequired:
                    form.add_error(
                        "contact_by_business",
                        "You need to agree to be contacted to send this message.",
                    )
                except leads.SchemaError as exc:
                    form.add_error(None, "; ".join(exc.errors))
                else:
                    return redirect("public-lead-sent", public_id=listing.public_id)
    else:
        form = LeadForm()

    # A fresh token on every render, including a re-render after an error.
    form.data = form.data.copy() if form.is_bound else form.data
    token = issue_token(now)
    if form.is_bound:
        form.data["form_token"] = token
    else:
        form.initial["form_token"] = token

    response = render(
        request,
        "public/lead_form.html",
        {
            "listing": listing,
            "form": form,
            "consent_text": leads.get_lead_consent_text(tenant),
        },
    )
    if limited:
        response = limit_responses.limited(response, limited)
    return _noindex(response)


@never_cache
@require_http_methods(["GET"])
def lead_sent(request, public_id):
    listing = _listing_or_404(request, public_id)
    return _noindex(render(request, "public/lead_sent.html", {"listing": listing}))
