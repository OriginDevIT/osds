"""Public claim submission and verification (spec §9, §9.0, §9.4, §9.5,
§9.6). Served under ``/claim/<public_id>/`` on a tenant's own domain, ahead
of the public-site catch-all (``osds/urls_tenant.py``).

Approval and anything writing ``Listing.status`` or ``Listing.owner`` live in
``directory.claim_review`` -- these views only ever collect the submission,
run domain_email verification, and hand off to ``directory.services``.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from audit.ratelimit import RateLimited
from directory import services
from directory.claims import domain_email_host
from directory.forms import ClaimForm, ClaimVerifyForm
from directory.masking import mask_email, mask_phone_e164
from directory.models import Claim, ClaimVerificationGuard, Listing
from osds.client_ip import client_ip as _client_ip


def _wait_text(seconds: int) -> str:
    """A wait as a human would say it, rounded up: never "0 minutes"."""
    if seconds < 60:
        return "a minute"
    minutes = -(-seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = -(-minutes // 60)
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _limited(response, exc: RateLimited):
    """Turn a rendered page into the 429 for ``exc``. The wait is the one fact
    the claimant needs; which rule fired is not shown."""
    response.status_code = 429
    response["Retry-After"] = str(exc.retry_after)
    return response


def _limit_message(exc: RateLimited) -> str:
    return f"Too many attempts. Please try again in {_wait_text(exc.retry_after)}."


def _fetch_guard(claim) -> "ClaimVerificationGuard | None":
    return ClaimVerificationGuard.objects.filter(
        listing=claim.listing, claimant=claim.claimant
    ).first()


def _ineligible_reason(claim) -> str:
    """Why domain_email wasn't used for ``claim`` -- a full sentence, not a
    fragment (decisions.md §4.4, §4.6). Read from the ``review_reason`` stored
    when the claim entered review, not recomputed: the page tells the claimant
    what happened to *this* claim, however the listing or the mail
    configuration has changed since. The stored reason is the one
    ``claims.domain_email_ineligibility`` produced at submit, so its
    precedence is the submit-time one: no eligible website, a locked pair,
    email verification unavailable, then an address not at the website's
    host."""
    reason = claim.review_reason
    if reason == "no_website":
        return (
            "This listing has no eligible website on file, so domain "
            "email verification wasn't available."
        )
    if reason == "locked":
        return (
            "Too many incorrect codes were entered for this address, so "
            "this claim will be reviewed manually."
        )
    if reason == "mail_unavailable":
        return (
            "Email verification is unavailable for this directory right "
            "now, so this claim will be reviewed manually."
        )
    if reason == "address_mismatch":
        host = domain_email_host(claim.listing)
        if host:
            return f"The claiming email address must be at @{host}."
    return "Domain email verification wasn't used, so this claim will be reviewed manually."


@require_http_methods(["GET", "POST"])
def claim_form(request, public_id):
    listing = get_object_or_404(Listing.objects.published(), public_id=public_id)
    tenant = request.tenant
    consent_text = services.get_default_consent_text(tenant)
    enabled_methods = services.enabled_claim_methods(tenant)

    limited = None
    if request.method == "POST":
        form = ClaimForm(
            request.POST, listing=listing, enabled_methods=enabled_methods
        )
        if form.is_valid():
            requested_method = form.cleaned_data["method"]
            try:
                claim = services.submit_claim(
                    tenant,
                    listing=listing,
                    method=requested_method,
                    claimant={
                        "name": form.cleaned_data["name"],
                        "email": form.cleaned_data["email"],
                        "phone_e164": form.cleaned_data["phone_e164"],
                        "role_claimed": form.cleaned_data["role_claimed"],
                    },
                    consent=form.consent_payload(),
                    ip=_client_ip(request),
                )
            except RateLimited as exc:
                limited = exc
                form.add_error(None, _limit_message(exc))
            except services.ConsentRequired:
                form.add_error(None, "Please respond to every consent option below.")
            except services.SchemaError as exc:
                form.add_error(None, "; ".join(exc.errors))
            else:
                # claim.method, not the requested method: domain_email
                # covers both "a code was sent" and "eligible but deferred
                # by a cooldown/cap" -- the verify page explains either.
                # Anything else -- including a domain_email request that
                # came back manual -- goes to submitted.
                if claim.method == Claim.Method.DOMAIN_EMAIL:
                    return redirect("public-claim-verify", public_id=claim.public_id)
                return redirect("public-claim-submitted", public_id=claim.public_id)
    else:
        form = ClaimForm(
            listing=listing,
            enabled_methods=enabled_methods,
            initial={"role_claimed": "owner"},
        )

    response = render(
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
    return _limited(response, limited) if limited else response


@require_http_methods(["GET"])
def claim_submitted(request, public_id):
    claim = get_object_or_404(Claim.objects, public_id=public_id)
    context = {"claim": claim}
    if (
        claim.requested_method == Claim.Method.DOMAIN_EMAIL
        and claim.method == Claim.Method.MANUAL
    ):
        context["ineligible_reason"] = _ineligible_reason(claim)
    return render(request, "public/claim_submitted.html", context)


def _verify_reason(claim, guard, now) -> "str | None":
    """Mirrors ``_apply_verify_claim_code``'s own check order (decisions.md
    §4.4 ruling 8), read-only, so a GET reflects the claim's current state
    without needing a failed POST first. ``None`` means verification is
    open: the code form applies."""
    if claim.status not in (Claim.Status.PENDING_VERIFICATION, Claim.Status.DISPUTED):
        return "status"
    if claim.method != Claim.Method.DOMAIN_EMAIL:
        return "method"
    if claim.verified_at is not None:
        return "verified"
    if guard is not None and guard.verification_locked_at is not None:
        return "locked"
    if (
        guard is not None
        and guard.cooldown_until is not None
        and guard.cooldown_until > now
    ):
        return "cooldown"
    if not claim.code_hash:
        return "no_active_code"
    if claim.expires_at is not None and claim.expires_at <= now:
        return "expired"
    return None


def _refusal_text(reason: str, *, claim, guard) -> str:
    """One message per VerificationRefused reason (decisions.md §4.4 ruling
    8: "the claimant is told exactly where they stand")."""
    if reason == "status":
        return f"This claim's status is {claim.get_status_display()}."
    if reason == "method":
        return f"This claim's verification method is {claim.get_method_display()}."
    if reason == "verified":
        return "This claim has already been verified."
    if reason == "locked":
        return "This claim will be reviewed manually."
    if reason == "cooldown":
        until = guard.cooldown_until if guard else None
        if until:
            return (
                "Too many incorrect codes. This code no longer works. You "
                f"can request a new one after {until:%Y-%m-%d %H:%M} UTC."
            )
        return "Too many incorrect codes. This code no longer works."
    if reason == "no_active_code":
        return "No verification code has been sent yet."
    if reason == "expired":
        return "This code has expired. Request a new one."
    if reason == "cap":
        return "The resend limit has been reached for now."
    return "This code can no longer be entered here."


# Reasons where neither the code form nor the resend form applies -- the
# claim itself is out of scope for verification (decisions.md §4.4 ruling
# 8: "status", "method" or "verified_at is set" show the state, no forms).
_NO_FORMS_REASONS = {"status", "method", "verified", "locked"}
# Reasons where a fresh code is still worth offering even though the
# current one can't be entered.
_OFFER_RESEND_REASONS = {"no_active_code", "expired"}


def _next_send_at(guard) -> "timezone.datetime | None":
    """Latest of the three independent caps that could still be blocking a
    send (spec §9.6): the 60-second floor, an active cooldown, and the
    24-hour window once it's exhausted."""
    if guard is None:
        return None
    candidates = []
    if guard.last_code_sent_at:
        candidates.append(
            guard.last_code_sent_at
            + timedelta(seconds=services.MIN_SECONDS_BETWEEN_SENDS)
        )
    if guard.cooldown_until:
        candidates.append(guard.cooldown_until)
    if (
        guard.codes_sent_in_window >= services.MAX_SENDS_PER_WINDOW
        and guard.codes_sent_window_start
    ):
        candidates.append(guard.codes_sent_window_start + services.SEND_WINDOW)
    return max(candidates) if candidates else None


def _verify_context(claim) -> dict:
    guard = _fetch_guard(claim)
    now = timezone.now()
    reason = _verify_reason(claim, guard, now)
    tries_left = None
    if guard is not None and reason not in _NO_FORMS_REASONS:
        tries_left = max(0, services.MAX_WRONG_ENTRIES - guard.wrong_entries)
    cooldowns_left = None
    if reason == "cooldown" and guard is not None:
        cooldowns_left = max(0, services.MAX_COOLDOWNS - guard.cooldowns)
    return {
        "destination": mask_email(claim.claimant.email),
        "reason": reason,
        "message": _refusal_text(reason, claim=claim, guard=guard) if reason else None,
        "cooldowns_left": cooldowns_left,
        "show_code_form": reason is None,
        "show_resend_form": reason is None or reason in _OFFER_RESEND_REASONS,
        "tries_left": tries_left,
        "expires_at": claim.expires_at,
        "next_send_at": _next_send_at(guard),
    }


@require_http_methods(["GET", "POST"])
def claim_verify(request, public_id):
    claim = get_object_or_404(Claim.objects, public_id=public_id)
    tenant = request.tenant
    form = ClaimVerifyForm(request.POST if request.method == "POST" else None)
    attempted = False
    limited = None

    if request.method == "POST" and form.is_valid():
        attempted = True
        try:
            claim, correct = services.verify_claim_code(
                tenant,
                claim=claim,
                code=form.cleaned_data["code"],
                ip=_client_ip(request),
            )
        except RateLimited as exc:
            limited = exc
        except services.VerificationRefused:
            pass  # the recomputed status below explains why
        else:
            if correct:
                return render(request, "public/claim_verified.html", {"claim": claim})

    status = _verify_context(claim)
    if limited:
        form.add_error(None, _limit_message(limited))
    elif attempted:
        if status["reason"]:
            form.add_error(None, status["message"])
        else:
            form.add_error(
                None,
                f"That code isn't right. {status['tries_left']} tries left "
                "before a 15-minute wait.",
            )

    response = render(
        request,
        "public/claim_verify.html",
        {"claim": claim, "form": form, "status": status},
    )
    return _limited(response, limited) if limited else response


@require_http_methods(["POST"])
def claim_verify_resend(request, public_id):
    claim = get_object_or_404(Claim.objects, public_id=public_id)
    tenant = request.tenant
    try:
        services.start_claim_verification(
            tenant, claim=claim, ip=_client_ip(request)
        )
    except RateLimited as exc:
        # A POST-only route with nothing of its own to render: show the verify
        # page, which already explains where the claimant stands, with the
        # refusal as a flash message like every other resend refusal.
        messages.error(request, _limit_message(exc))
        return _limited(
            render(
                request,
                "public/claim_verify.html",
                {
                    "claim": claim,
                    "form": ClaimVerifyForm(),
                    "status": _verify_context(claim),
                },
            ),
            exc,
        )
    except services.VerificationRefused as exc:
        messages.error(
            request, _refusal_text(exc.reason, claim=claim, guard=_fetch_guard(claim))
        )
    return redirect("public-claim-verify", public_id=public_id)
