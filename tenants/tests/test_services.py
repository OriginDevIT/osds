"""tenants.services command orchestrators (spec §11.2 shape).

``create_tenant`` / ``create_operator`` / ``invite_staff`` write the command
log outside the state-change transaction, so they refuse to run inside one --
the guard tests exploit the transaction a plain ``TestCase`` opens, the rest
use ``TransactionTestCase``.
"""

from __future__ import annotations

from django.db import IntegrityError
from django.test import TestCase, TransactionTestCase

from audit.models import CommandLog, OutboxEvent
from tenants.models import Operator, StaffMembership, Tenant
from tenants.services import (
    MustNotBeInTransaction,
    create_operator,
    create_tenant,
    invite_staff,
)


class OrchestratorTransactionGuardTests(TestCase):
    """Plan #13: a plain ``TestCase`` wraps every test in a transaction, which
    is exactly what the orchestrators forbid."""

    @classmethod
    def setUpTestData(cls):
        cls.op = Operator.objects.create_superuser(
            email="root@example.test", password="x"
        )
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def test_create_tenant_refuses_an_open_transaction(self):
        with self.assertRaises(MustNotBeInTransaction):
            create_tenant(name="A", slug="a", mode="single", created_by=self.op)

    def test_create_operator_refuses_an_open_transaction(self):
        with self.assertRaises(MustNotBeInTransaction):
            create_operator(email="new@example.test", created_by=self.op)

    def test_invite_staff_refuses_an_open_transaction(self):
        with self.assertRaises(MustNotBeInTransaction):
            invite_staff(
                tenant=self.tenant,
                email="new@example.test",
                role=StaffMembership.Role.EDITOR,
                invited_by=self.op,
            )


class _Base(TransactionTestCase):
    def setUp(self):
        self.op = Operator.objects.create_superuser(
            email="root@example.test", password="x"
        )
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")


class CreateTenantTests(_Base):
    def test_applied_row_has_a_null_tenant_and_the_event_id(self):
        tenant = create_tenant(
            name="Chicago Plumbers",
            slug="chicago-plumbers",
            mode="single",
            created_by=self.op,
        )
        event = OutboxEvent.all_tenants.get(type="tenant.created")
        self.assertEqual(event.subject, tenant.public_id)

        row = CommandLog.objects.get(command="tenant.create")
        self.assertIsNone(row.tenant_id)  # #164: never backfilled
        self.assertEqual(row.outcome, "applied")
        self.assertEqual(row.result_event_id, event.event_id)
        self.assertIsNotNone(row.concluded_at)


class CreateOperatorTests(_Base):
    def test_emits_nothing_and_logs_a_null_tenant_applied_row(self):
        events_before = OutboxEvent.all_tenants.count()

        operator = create_operator(
            email="  Dana@Example.TEST ", name="Dana", created_by=self.op
        )

        self.assertEqual(OutboxEvent.all_tenants.count(), events_before)
        self.assertEqual(operator.email, "dana@example.test")
        self.assertFalse(operator.has_usable_password())

        row = CommandLog.objects.get(command="operator.create")
        self.assertIsNone(row.tenant_id)
        self.assertEqual(row.outcome, "applied")
        self.assertEqual(row.result_event_id, "")
        self.assertIsNotNone(row.concluded_at)


class InviteStaffTests(_Base):
    def test_an_existing_operator_row_is_never_written(self):
        """Plan #7: snapshot every column, invite, compare."""
        existing = Operator.objects.create(
            email="dana@example.test", name="Dana"
        )
        existing.set_unusable_password()
        existing.save()
        before = Operator.objects.filter(pk=existing.pk).values()[0]

        invite_staff(
            tenant=self.tenant,
            email="Dana@example.test",  # different case -> same row
            role=StaffMembership.Role.EDITOR,
            invited_by=self.op,
        )

        after = Operator.objects.filter(pk=existing.pk).values()[0]
        self.assertEqual(before, after)

        membership = StaffMembership.objects.get()
        self.assertEqual(membership.operator_id, existing.pk)
        self.assertEqual(membership.status, StaffMembership.Status.PENDING)
        self.assertEqual(
            OutboxEvent.all_tenants.get(type="staff.invited")
            .data["operator"]["existing"],
            True,
        )

    def test_one_staff_invited_pending_and_no_staff_accepted(self):
        """Plan #8."""
        invite_staff(
            tenant=self.tenant,
            email="dana@example.test",
            role=StaffMembership.Role.EDITOR,
            invited_by=self.op,
        )

        invited = OutboxEvent.all_tenants.filter(type="staff.invited")
        self.assertEqual(invited.count(), 1)
        self.assertEqual(invited.get().data["membership"]["status"], "pending")
        self.assertEqual(invited.get().data["membership"]["role"], "editor")
        self.assertEqual(invited.get().data["operator"]["existing"], False)
        self.assertEqual(
            OutboxEvent.all_tenants.filter(type="staff.accepted").count(), 0
        )
        self.assertEqual(
            StaffMembership.objects.get().status,
            StaffMembership.Status.PENDING,
        )

    def test_duplicate_membership_is_rejected_and_the_connection_survives(self):
        kw = dict(
            tenant=self.tenant,
            email="dana@example.test",
            role=StaffMembership.Role.EDITOR,
            invited_by=self.op,
        )
        invite_staff(**kw)

        with self.assertRaises(IntegrityError):
            invite_staff(**kw)

        rejected = CommandLog.objects.filter(
            command="staff.invite", outcome="rejected"
        )
        self.assertEqual(rejected.count(), 1)
        self.assertEqual(
            rejected.get().problem, {"error": "duplicate_membership"}
        )
        # the rolled-back apply left nothing behind
        self.assertEqual(StaffMembership.objects.count(), 1)
        self.assertEqual(
            OutboxEvent.all_tenants.filter(type="staff.invited").count(), 1
        )
        # the connection is still usable: a different invite still goes through
        invite_staff(
            tenant=self.tenant,
            email="lee@example.test",
            role=StaffMembership.Role.SUPPORT,
            invited_by=self.op,
        )
        self.assertEqual(StaffMembership.objects.count(), 2)
        self.assertEqual(
            CommandLog.objects.filter(
                command="staff.invite", outcome="applied"
            ).count(),
            2,
        )
