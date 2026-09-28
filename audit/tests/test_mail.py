"""``audit.mail.enqueue`` (decisions.md §4.3 "Outbound mail")."""

from __future__ import annotations

from django.db import transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from audit.mail import MustBeInTransaction, enqueue
from audit.models import OutboundMessage
from tenants.models import Tenant


class EnqueueWritesTests(TestCase):
    """``TestCase`` already runs each test inside an atomic block, which is
    exactly the state ``enqueue`` requires -- no need for a real autocommit
    connection here."""

    def setUp(self):
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def test_enqueue_writes_a_pending_row(self):
        with transaction.atomic():
            message = enqueue(
                tenant=self.tenant,
                kind="claim.verification_code",
                to_address="claimant@example.test",
                subject="Your code",
                body_text="123456",
            )
        message.refresh_from_db()
        self.assertEqual(message.status, OutboundMessage.Status.PENDING)
        self.assertEqual(message.attempt, 0)
        self.assertEqual(message.tenant_id, self.tenant.id)
        self.assertEqual(message.kind, "claim.verification_code")
        self.assertEqual(message.to_address, "claimant@example.test")
        self.assertEqual(message.body_text, "123456")
        self.assertIsNone(message.expires_at)
        self.assertLessEqual(message.next_attempt_at, timezone.now())

    def test_enqueue_stamps_expires_at_from_the_caller_when_given(self):
        deadline = timezone.now() + timezone.timedelta(minutes=30)
        with transaction.atomic():
            message = enqueue(
                tenant=self.tenant,
                kind="claim.verification_code",
                to_address="claimant@example.test",
                subject="Your code",
                body_text="123456",
                expires_at=deadline,
            )
        message.refresh_from_db()
        self.assertEqual(message.expires_at, deadline)

    def test_enqueue_leaves_expires_at_null_when_omitted(self):
        with transaction.atomic():
            message = enqueue(
                tenant=self.tenant,
                kind="claim.verification_code",
                to_address="claimant@example.test",
                subject="Your code",
                body_text="123456",
            )
        message.refresh_from_db()
        self.assertIsNone(message.expires_at)


class EnqueueAtomicityTests(TransactionTestCase):
    """The autocommit / rollback behaviour needs a real connection --
    ``TestCase`` wraps every test in an atomic block, which would make
    ``require_atomic`` pass unconditionally (same reasoning as
    ``audit.tests.test_command_log.AutocommitTests``)."""

    def setUp(self):
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def test_raises_outside_an_atomic_block(self):
        self.assertFalse(transaction.get_connection().in_atomic_block)
        with self.assertRaises(MustBeInTransaction):
            enqueue(
                tenant=self.tenant,
                kind="claim.verification_code",
                to_address="claimant@example.test",
                subject="Your code",
                body_text="123456",
            )
        self.assertEqual(OutboundMessage.all_tenants.count(), 0)

    def test_passes_inside_an_atomic_block(self):
        with transaction.atomic():
            enqueue(
                tenant=self.tenant,
                kind="claim.verification_code",
                to_address="claimant@example.test",
                subject="Your code",
                body_text="123456",
            )
        self.assertEqual(OutboundMessage.all_tenants.count(), 1)

    def test_rollback_of_the_caller_s_transaction_discards_the_row(self):
        with self.assertRaises(ValueError):
            with transaction.atomic():
                enqueue(
                    tenant=self.tenant,
                    kind="claim.verification_code",
                    to_address="claimant@example.test",
                    subject="Your code",
                    body_text="123456",
                )
                raise ValueError("caller's state change failed")
        self.assertEqual(OutboundMessage.all_tenants.count(), 0)
