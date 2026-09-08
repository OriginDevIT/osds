"""#146 -- Tenant, Operator and StaffMembership in the console Django admin,
writing through ``tenants.services``.

The admin's ``add_view`` calls a command orchestrator, which refuses to run
inside an open transaction, so the POST tests use ``TransactionTestCase``; a
plain ``TestCase`` wraps every test in one.
"""

from __future__ import annotations

from unittest import mock

from django.contrib import admin
from django.contrib.messages import get_messages
from django.db import connection
from django.forms import Form
from django.forms.models import BaseModelForm
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import reverse
from django.utils import timezone

from audit.models import CommandLog, OutboxEvent
from tenants import services
from tenants.admin import OperatorAdmin, StaffMembershipAdmin, TenantAdmin
from tenants.admin_forms import OperatorAddForm, StaffInviteForm, TenantAddForm
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant

CONSOLE = "console.test"
_FAST_HASH = ["django.contrib.auth.hashers.MD5PasswordHasher"]
_UC = "osds.urls_console"

ADD_TENANT = reverse("admin:tenants_tenant_add", urlconf=_UC)
ADD_OPERATOR = reverse("admin:tenants_operator_add", urlconf=_UC)
ADD_STAFF = reverse("admin:tenants_staffmembership_add", urlconf=_UC)
TENANT_CHANGELIST = reverse("admin:tenants_tenant_changelist", urlconf=_UC)
OPERATOR_CHANGELIST = reverse("admin:tenants_operator_changelist", urlconf=_UC)
STAFF_CHANGELIST = reverse("admin:tenants_staffmembership_changelist", urlconf=_UC)


# --------------------------------------------------------------------------
# Introspection -- no database
# --------------------------------------------------------------------------
class FormShapeTests(SimpleTestCase):
    """Plan #11."""

    def test_add_forms_are_plain_forms_not_modelforms(self):
        for form_cls in (TenantAddForm, OperatorAddForm, StaffInviteForm):
            self.assertTrue(issubclass(form_cls, Form), form_cls)
            self.assertFalse(issubclass(form_cls, BaseModelForm), form_cls)

    def test_admin_add_form_class_is_the_plain_form(self):
        self.assertIs(admin.site._registry[Tenant].add_form_class, TenantAddForm)
        self.assertIs(
            admin.site._registry[Operator].add_form_class, OperatorAddForm
        )
        self.assertIs(
            admin.site._registry[StaffMembership].add_form_class, StaffInviteForm
        )

    def test_operator_form_has_no_is_superadmin_field(self):  # #165
        self.assertNotIn("is_superadmin", OperatorAddForm().fields)


class RegistrationTests(SimpleTestCase):
    """Plan #12."""

    def test_the_three_principal_models_are_registered(self):
        for model in (Tenant, Operator, StaffMembership):
            self.assertIn(model, admin.site._registry)

    def test_no_directory_or_billing_model_is_registered(self):
        for model in admin.site._registry:
            self.assertNotIn(
                model._meta.app_label, {"directory", "billing"}, model
            )

    def test_tenants_app_registers_exactly_the_three(self):
        registered = {
            m for m in admin.site._registry if m._meta.app_label == "tenants"
        }
        self.assertEqual(registered, {Tenant, Operator, StaffMembership})


class SaveHookTests(SimpleTestCase):
    """Plan #3 -- a raw admin save must never reach the ORM here."""

    def test_save_model_raises(self):
        for model in (Tenant, Operator, StaffMembership):
            ma = admin.site._registry[model]
            with self.assertRaises(NotImplementedError):
                ma.save_model(request=None, obj=None, form=None, change=False)

    def test_save_related_raises(self):
        for model in (Tenant, Operator, StaffMembership):
            ma = admin.site._registry[model]
            with self.assertRaises(NotImplementedError):
                ma.save_related(
                    request=None, form=None, formsets=[], change=False
                )


class SensitiveFieldTests(SimpleTestCase):
    """password / last_login / is_staff / is_superuser stay off every list."""

    FORBIDDEN = {"password", "last_login", "is_staff", "is_superuser"}

    def test_operator_admin_never_exposes_them(self):
        ma = admin.site._registry[Operator]
        for attr in ("list_display", "fields", "readonly_fields"):
            self.assertEqual(
                set(getattr(ma, attr)) & self.FORBIDDEN, set(), attr
            )


# --------------------------------------------------------------------------
# Permissions -- GET only, plain TestCase
# --------------------------------------------------------------------------
@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST=CONSOLE, PASSWORD_HASHERS=_FAST_HASH
)
class PermissionTests(TestCase):
    """Plan #10."""

    def setUp(self):
        InstallSetup.objects.create(
            token_hash="x" * 64, completed_at=timezone.now()
        )
        self.superadmin = Operator.objects.create_superuser(
            email="root@example.test", password="pw"
        )
        self.staff_only = Operator.objects.create(
            email="staff@example.test",
            is_staff=True,
            is_superadmin=False,
            is_active=True,
        )
        self.staff_only.set_unusable_password()
        self.staff_only.save()

    def _client(self, operator):
        c = Client()
        c.force_login(operator)
        return c

    def test_non_superadmin_staff_is_403_on_every_page(self):
        c = self._client(self.staff_only)
        for url in (
            ADD_TENANT,
            ADD_OPERATOR,
            ADD_STAFF,
            TENANT_CHANGELIST,
            OPERATOR_CHANGELIST,
            STAFF_CHANGELIST,
        ):
            self.assertEqual(
                c.get(url, HTTP_HOST=CONSOLE).status_code, 403, url
            )

    def test_superadmin_reaches_the_add_pages(self):
        c = self._client(self.superadmin)
        for url in (ADD_TENANT, ADD_OPERATOR, ADD_STAFF):
            self.assertEqual(
                c.get(url, HTTP_HOST=CONSOLE).status_code, 200, url
            )

    def test_hooks_gate_on_is_superadmin_and_close_change_delete(self):
        req = RequestFactory().get("/")
        for model in (Tenant, Operator, StaffMembership):
            ma = admin.site._registry[model]

            req.user = self.staff_only
            self.assertFalse(ma.has_add_permission(req), model)
            self.assertFalse(ma.has_view_permission(req), model)
            self.assertFalse(ma.has_module_permission(req), model)

            req.user = self.superadmin
            self.assertTrue(ma.has_add_permission(req), model)
            self.assertTrue(ma.has_view_permission(req), model)
            self.assertTrue(ma.has_module_permission(req), model)
            self.assertFalse(ma.has_change_permission(req), model)
            self.assertFalse(ma.has_delete_permission(req), model)


# --------------------------------------------------------------------------
# add_view POSTs -- hit the service, need TransactionTestCase
# --------------------------------------------------------------------------
@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST=CONSOLE, PASSWORD_HASHERS=_FAST_HASH
)
class AddViewTests(TransactionTestCase):
    def setUp(self):
        InstallSetup.objects.create(
            token_hash="x" * 64, completed_at=timezone.now()
        )
        self.superadmin = Operator.objects.create_superuser(
            email="root@example.test", password="pw"
        )
        self.client = Client()
        self.client.force_login(self.superadmin)

    def _post(self, url, data, **extra):
        return self.client.post(url, data, HTTP_HOST=CONSOLE, **extra)

    # ---- #4 ----
    def test_tenant_add_emits_exactly_one_tenant_created(self):
        resp = self._post(
            ADD_TENANT,
            {"name": "Chicago Plumbers", "slug": "chi", "mode": "single"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], TENANT_CHANGELIST)

        tenant = Tenant.objects.get(slug="chi")
        events = OutboxEvent.all_tenants.filter(type="tenant.created")
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().subject, tenant.public_id)
        self.assertEqual(
            events.get().actor,
            {"type": "admin", "id": self.superadmin.public_id},
        )

    # ---- #5 ----
    def test_operator_add_emits_nothing_and_logs_a_null_tenant_row(self):
        events_before = OutboxEvent.all_tenants.count()

        resp = self._post(
            ADD_OPERATOR, {"email": "Dana@Example.test", "name": "Dana"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], OPERATOR_CHANGELIST)
        self.assertEqual(OutboxEvent.all_tenants.count(), events_before)

        operator = Operator.objects.get(email="dana@example.test")
        self.assertFalse(operator.has_usable_password())

        row = CommandLog.objects.get(command="operator.create")
        self.assertIsNone(row.tenant_id)
        self.assertEqual(row.outcome, "applied")

    # ---- #1 ----
    def test_command_log_row_survives_a_failed_apply(self):
        client = Client(raise_request_exception=False)
        client.force_login(self.superadmin)
        with mock.patch(
            "tenants.services._apply_create_tenant",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post(
                ADD_TENANT,
                {"name": "X", "slug": "x", "mode": "single"},
                HTTP_HOST=CONSOLE,
            )
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(Tenant.objects.filter(slug="x").exists())
        self.assertTrue(
            CommandLog.objects.filter(
                command="tenant.create", outcome__isnull=True
            ).exists()
        )

    # ---- #2 ----
    def test_service_runs_outside_a_transaction(self):
        seen: list[bool] = []
        real = services._apply_create_tenant

        def spy(**kwargs):
            seen.append(connection.in_atomic_block)
            return real(**kwargs)

        with mock.patch(
            "tenants.services._apply_create_tenant", side_effect=spy
        ):
            resp = self._post(
                ADD_TENANT, {"name": "Y", "slug": "y", "mode": "single"}
            )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(seen, [False])

    # ---- #3, POST half ----
    def test_post_never_calls_save_model(self):
        with mock.patch.object(
            TenantAdmin, "save_model", side_effect=AssertionError("called")
        ) as m:
            resp = self._post(
                ADD_TENANT, {"name": "Z", "slug": "z", "mode": "single"}
            )
        self.assertEqual(resp.status_code, 302)
        m.assert_not_called()

    # ---- #6 ----
    def test_invitation_response_is_identical_for_existing_and_new_email(self):
        ta = Tenant.objects.create(slug="ta", name="TA")
        tb = Tenant.objects.create(slug="tb", name="TB")
        email = "dana@example.test"
        role = str(int(StaffMembership.Role.EDITOR))

        # branch 1: the operator already exists, and already administers tb
        existing = Operator.objects.create(email=email, name="Dana")
        existing.set_unusable_password()
        existing.save()
        StaffMembership.objects.create(
            operator=existing,
            tenant=tb,
            role=StaffMembership.Role.SUPPORT,
            status=StaffMembership.Status.ACTIVE,
        )
        c1 = Client()
        c1.force_login(self.superadmin)
        r1 = c1.post(
            ADD_STAFF,
            {"tenant": ta.pk, "email": email, "role": role},
            HTTP_HOST=CONSOLE,
        )
        msgs1 = [(m.level, m.message) for m in get_messages(r1.wsgi_request)]

        # reset to the pre-invite state; same email, operator now absent
        StaffMembership.objects.all().delete()
        Operator.objects.filter(email=email).delete()

        # branch 2: no operator for this email
        c2 = Client()
        c2.force_login(self.superadmin)
        r2 = c2.post(
            ADD_STAFF,
            {"tenant": ta.pk, "email": email, "role": role},
            HTTP_HOST=CONSOLE,
        )
        msgs2 = [(m.level, m.message) for m in get_messages(r2.wsgi_request)]

        self.assertEqual(r1.status_code, r2.status_code)
        self.assertEqual(r1.status_code, 302)
        self.assertEqual(r1["Location"], r2["Location"])
        self.assertEqual(r1.content, r2.content)
        self.assertEqual(msgs1, msgs2)
        self.assertEqual(
            StaffMembership.objects.filter(
                status=StaffMembership.Status.PENDING
            ).count(),
            1,
        )
