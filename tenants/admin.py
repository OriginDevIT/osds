"""Console admin for the principal and structural models (#146).

``Tenant``, ``Operator`` and ``StaffMembership`` are not tenant data
(decisions.md section 4): ``Tenant`` *is* the tenant, an ``Operator`` spans the
installation, and a ``StaffMembership`` *is* the operator-tenant link. They are
the one thing the console Django admin writes.

A raw ``ModelAdmin`` save emits nothing and skips the command log, so every
create here goes through ``tenants.services``. ``add_view`` is replaced
wholesale -- it never calls ``super()``, so it never enters
``ModelAdmin._changeform_view`` and its wrapping ``transaction.atomic`` -- it
builds a plain ``forms.Form`` and calls the service with the acting operator
passed explicitly from ``request.user``. Change and delete are closed: a role
change, a suspension and an operator deletion are each their own service
function and their own event, out of scope for #146.

Every permission hook is gated on ``request.user.is_superadmin``. Each act here
happens outside any tenant and no membership can authorise it (spec section
4.4); the ``AdminSite`` login gate stays ``is_staff``.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect

from tenants import services
from tenants.admin_forms import OperatorAddForm, StaffInviteForm, TenantAddForm
from tenants.models import Operator, StaffMembership, Tenant

_ADD_TEMPLATE = "admin/tenants/principal_add_form.html"


class PrincipalAdmin(admin.ModelAdmin):
    """Read-only list + detail, plus a service-backed add. No changeform, no
    delete. Subclasses set ``add_form_class`` and implement ``run_service`` /
    ``added_message`` / ``rejection_message``.
    """

    add_form_class: type | None = None
    rejection_message = "That record already exists."

    # -- permissions: installation scope only (spec section 4.4) --------------
    def has_module_permission(self, request):
        return bool(getattr(request.user, "is_superadmin", False))

    def has_view_permission(self, request, obj=None):
        return bool(getattr(request.user, "is_superadmin", False))

    def has_add_permission(self, request):
        return bool(getattr(request.user, "is_superadmin", False))

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    # -- a raw admin save must never reach the ORM on this surface -----------
    def save_model(self, request, obj, form, change):
        raise NotImplementedError(
            "console admin writes go through tenants.services, not save_model"
        )

    def save_related(self, request, form, formsets, change):
        raise NotImplementedError(
            "console admin writes go through tenants.services, not save_related"
        )

    # -- add: plain Form -> service function --------------------------------
    @method_decorator(csrf_protect)
    def add_view(self, request, form_url="", extra_context=None):
        if not self.has_add_permission(request):
            raise PermissionDenied

        form = self.add_form_class(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                self.run_service(request, form.cleaned_data)
            except IntegrityError:
                form.add_error(None, self.rejection_message)
            else:
                self.message_user(
                    request,
                    self.added_message(form.cleaned_data),
                    messages.SUCCESS,
                )
                return redirect(
                    "admin:%s_%s_changelist"
                    % (self.opts.app_label, self.opts.model_name)
                )

        context = {
            **self.admin_site.each_context(request),
            **(extra_context or {}),
            "title": f"Add {self.opts.verbose_name}",
            "opts": self.opts,
            "app_label": self.opts.app_label,
            "form": form,
            "form_url": form_url,
        }
        return TemplateResponse(request, _ADD_TEMPLATE, context)

    # -- subclass hooks ----------------------------------------------------
    def run_service(self, request, data):  # pragma: no cover - overridden
        raise NotImplementedError

    def added_message(self, data):  # pragma: no cover - overridden
        raise NotImplementedError


@admin.register(Tenant)
class TenantAdmin(PrincipalAdmin):
    add_form_class = TenantAddForm
    rejection_message = "A directory with that slug already exists."

    list_display = (
        "public_id",
        "slug",
        "name",
        "mode",
        "status",
        "primary_domain",
        "domain_verified_at",
        "created_at",
    )
    fields = (
        "public_id",
        "slug",
        "name",
        "mode",
        "status",
        "primary_domain",
        "domain_verified_at",
        "settings",
        "created_by",
        "created_at",
    )
    readonly_fields = (
        "public_id",
        "slug",
        "name",
        "mode",
        "status",
        "primary_domain",
        "domain_verified_at",
        "settings",
        "created_by",
        "created_at",
    )
    ordering = ("-created_at",)

    def run_service(self, request, data):
        services.create_tenant(
            name=data["name"],
            slug=data["slug"],
            mode=data["mode"],
            created_by=request.user,
        )

    def added_message(self, data):
        return "Directory '%s' created." % data["slug"]


@admin.register(Operator)
class OperatorAdmin(PrincipalAdmin):
    add_form_class = OperatorAddForm
    rejection_message = "An operator with that email already exists."

    # password / last_login / is_staff / is_superuser deliberately absent.
    list_display = (
        "public_id",
        "email",
        "name",
        "is_superadmin",
        "is_active",
        "created_at",
    )
    fields = (
        "public_id",
        "email",
        "name",
        "is_superadmin",
        "is_active",
        "created_at",
    )
    readonly_fields = (
        "public_id",
        "email",
        "name",
        "is_superadmin",
        "is_active",
        "created_at",
    )
    ordering = ("-created_at",)

    def run_service(self, request, data):
        services.create_operator(
            email=data["email"],
            name=data.get("name", ""),
            created_by=request.user,
        )

    def added_message(self, data):
        return "Operator '%s' created." % data["email"].strip().lower()


@admin.register(StaffMembership)
class StaffMembershipAdmin(PrincipalAdmin):
    add_form_class = StaffInviteForm
    rejection_message = (
        "That operator already has a membership on that directory."
    )

    list_display = (
        "operator",
        "tenant",
        "role",
        "status",
        "invited_by",
        "created_at",
        "accepted_at",
    )
    fields = (
        "operator",
        "tenant",
        "role",
        "status",
        "invited_by",
        "created_at",
        "accepted_at",
    )
    readonly_fields = (
        "operator",
        "tenant",
        "role",
        "status",
        "invited_by",
        "created_at",
        "accepted_at",
    )
    ordering = ("-created_at",)

    def run_service(self, request, data):
        services.invite_staff(
            tenant=data["tenant"],
            email=data["email"],
            role=data["role"],
            invited_by=request.user,
        )

    def added_message(self, data):
        # Identical whether or not the email already had an account (spec
        # section 4.4): the acting operator only ever sees the address they
        # typed.
        return "Invitation sent to '%s'." % data["email"].strip().lower()
