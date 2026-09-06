"""First-run wizard.

Every step writes through ``tenants.services`` as it is completed -- there is
no save-at-the-end. The current step is derived from the data
(``tenants.setup_state.next_step``), so closing the browser and coming back
(any device, any app process) resumes at the first unfinished step.

The route disappears the moment ``InstallSetup.completed_at`` is set: every
view here raises ``Http404`` once setup is complete, and
``TenantResolutionMiddleware`` stops routing to this URLconf.
"""

from __future__ import annotations

import hashlib
import hmac

from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render

from tenants import services
from tenants.dns_check import CHALLENGE_PATH, check_domain_http
from tenants.models import InstallSetup, Operator, Tenant
from tenants.secrets import set_secret
from tenants.setup_state import next_step
from tenants.wizard import forms

_MAX_TOKEN_ATTEMPTS = 20


def _row_or_404() -> InstallSetup:
    row = InstallSetup.load()
    if row is None or row.completed_at is not None:
        raise Http404("first-run setup is not available")
    return row


def _unlocked(request) -> bool:
    return bool(request.session.get("setup_unlocked"))


def _first_operator():
    return Operator.objects.order_by("id").first()


def _first_tenant():
    return Tenant.objects.order_by("id").first()


def index(request):
    _row_or_404()
    if not _unlocked(request):
        return redirect("setup-unlock")
    return redirect(f"setup-{next_step()}")


def unlock(request):
    row = _row_or_404()
    attempts = request.session.get("setup_token_attempts", 0)
    if attempts >= _MAX_TOKEN_ATTEMPTS:
        return HttpResponse("Too many attempts. Restart the container.", status=429)

    form = forms.SetupTokenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        supplied = hashlib.sha256(
            form.cleaned_data["token"].encode("utf-8")
        ).hexdigest()
        if hmac.compare_digest(supplied, row.token_hash):
            request.session["setup_unlocked"] = True
            request.session.pop("setup_token_attempts", None)
            return redirect("setup-index")
        request.session["setup_token_attempts"] = attempts + 1
        form.add_error("token", "That token is not correct.")
    return render(request, "setup/unlock.html", {"form": form})


def account(request):
    _row_or_404()
    if not _unlocked(request):
        return redirect("setup-unlock")
    if Operator.objects.exists():
        return redirect("setup-index")

    form = forms.AccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        services.create_superadmin(
            email=form.cleaned_data["email"],
            password=form.cleaned_data["password1"],
            name=form.cleaned_data["name"],
        )
        return redirect("setup-index")
    return render(request, "setup/form.html", {"form": form, "step": "account",
                                               "title": "Create the superadmin"})


def directory(request):
    _row_or_404()
    if not _unlocked(request):
        return redirect("setup-unlock")
    operator = _first_operator()
    if operator is None:
        return redirect("setup-index")
    if Tenant.objects.exists():
        return redirect("setup-index")

    form = forms.DirectoryForm(request.POST or None, initial={"mode": Tenant.Mode.SINGLE})
    if request.method == "POST" and form.is_valid():
        tenant = services.create_tenant(
            name=form.cleaned_data["name"],
            slug=form.cleaned_data["slug"],
            mode=form.cleaned_data["mode"],
            created_by=operator,
        )
        services.add_bootstrap_membership(operator=operator, tenant=tenant)
        return redirect("setup-index")
    return render(request, "setup/form.html", {"form": form, "step": "directory",
                                               "title": "Your first directory"})


def domain(request):
    _row_or_404()
    if not _unlocked(request):
        return redirect("setup-unlock")
    tenant = _first_tenant()
    operator = _first_operator()
    if tenant is None or operator is None:
        return redirect("setup-index")

    form = forms.DomainForm(
        request.POST or None, initial={"domain": tenant.primary_domain or ""}
    )
    dns_detail = None
    if request.method == "POST" and form.is_valid():
        services.set_tenant_domain(
            tenant=tenant, domain=form.cleaned_data["domain"], changed_by=operator
        )
        tenant.refresh_from_db()
        if request.POST.get("action") == "verify":
            ok, dns_detail = check_domain_http(
                tenant.primary_domain, tenant.settings.get("domain_challenge", "")
            )
            if ok:
                services.mark_domain_verified(
                    tenant=tenant, method="http", verified_by=operator
                )
                return redirect("setup-index")
        else:
            return redirect("setup-index")

    return render(
        request,
        "setup/domain.html",
        {
            "form": form,
            "step": "domain",
            "title": "Point a domain at the directory",
            "tenant": tenant,
            "challenge_path": CHALLENGE_PATH,
            "challenge_token": tenant.settings.get("domain_challenge", ""),
            "dns_detail": dns_detail,
            "verified": tenant.domain_verified_at is not None,
        },
    )


def storage(request):
    _row_or_404()
    if not _unlocked(request):
        return redirect("setup-unlock")
    tenant, operator = _first_tenant(), _first_operator()
    if tenant is None or operator is None or not tenant.primary_domain:
        return redirect("setup-index")

    form = forms.StorageForm(request.POST or None, initial={"backend": "local"})
    if request.method == "POST" and form.is_valid():
        cfg = {
            "backend": form.cleaned_data["backend"],
            "bucket": form.cleaned_data["bucket"],
            "endpoint": form.cleaned_data["endpoint"],
            "access_key": form.cleaned_data["access_key"],
        }
        if form.cleaned_data["secret_key"]:
            set_secret("storage_secret_key", form.cleaned_data["secret_key"], tenant=tenant)
        services.update_tenant_settings(
            tenant=tenant, changes={"storage": cfg}, changed_by=operator
        )
        return redirect("setup-index")
    return render(request, "setup/form.html", {"form": form, "step": "storage",
                                               "title": "Media storage"})


def smtp(request):
    _row_or_404()
    if not _unlocked(request):
        return redirect("setup-unlock")
    tenant, operator = _first_tenant(), _first_operator()
    if tenant is None or operator is None or "storage" not in (tenant.settings or {}):
        return redirect("setup-index")

    form = forms.SmtpForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cfg = {
            "host": form.cleaned_data["host"],
            "port": form.cleaned_data["port"],
            "from_email": form.cleaned_data["from_email"],
            "username": form.cleaned_data["username"],
            "use_tls": form.cleaned_data["use_tls"],
        }
        if form.cleaned_data["password"]:
            set_secret("smtp_password", form.cleaned_data["password"], tenant=tenant)
        services.update_tenant_settings(
            tenant=tenant, changes={"smtp": cfg}, changed_by=operator
        )
        return redirect("setup-index")
    return render(request, "setup/form.html", {"form": form, "step": "smtp",
                                               "title": "Outgoing email (SMTP)"})


def claims(request):
    _row_or_404()
    if not _unlocked(request):
        return redirect("setup-unlock")
    tenant, operator = _first_tenant(), _first_operator()
    if tenant is None or operator is None or "smtp" not in (tenant.settings or {}):
        return redirect("setup-index")

    form = forms.ClaimsForm(request.POST or None, initial={"methods": ["manual"]})
    if request.method == "POST" and form.is_valid():
        cfg = {
            "enabled_methods": form.cleaned_data["methods"],
            "ttl": {"domain_email_minutes": form.cleaned_data["domain_email_ttl_minutes"]},
        }
        services.update_tenant_settings(
            tenant=tenant, changes={"claim_verification": cfg}, changed_by=operator
        )
        return redirect("setup-index")
    return render(request, "setup/form.html", {"form": form, "step": "claims",
                                               "title": "Claim verification"})


def done(request):
    _row_or_404()
    if not _unlocked(request):
        return redirect("setup-unlock")
    if next_step() != "done":
        return redirect("setup-index")  # an earlier step is still unfinished
    tenant = _first_tenant()
    if request.method == "POST":
        services.complete_setup()
        return render(request, "setup/complete.html", {"tenant": tenant})
    return render(request, "setup/done.html", {"tenant": tenant, "step": "done",
                                               "title": "Finish setup"})
