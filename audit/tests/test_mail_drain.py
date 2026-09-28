"""The mail drain (decisions.md §4.3 "Outbound mail").

Mirrors ``audit/tests/test_drain.py``'s shape: the drain does two short
transactions per attempt with the handler run unlocked between them, so
these tests use ``TransactionTestCase``. Time is injected via ``now=``.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

from audit.mail import enqueue as enqueue_message
from audit.models import OutboundMessage
from audit.worker import mail_drain
from audit.worker.mail_drain import (
    CLAIM_VISIBILITY_SECONDS,
    UNCONFIGURED_RECHECK,
    _record,
    attempt_message,
    mail_drain_once,
)
from osds.adapters import CapabilityUnconfigured, Result, override_capability
from osds.tenancy import get_current_tenant, tenant_context
from tenants.models import Tenant

HOUR = timedelta(hours=1)


# --------------------------------------------------------------------------
# test doubles
# --------------------------------------------------------------------------
class ScriptedProvider:
    """Returns a pre-set verb per call: ``ok`` | ``retry`` | ``permanent``.
    Records the tenant in scope on each call."""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.seen_tenants = []

    def send(self, message) -> Result:
        self.seen_tenants.append(get_current_tenant())
        verb = self._script[self._i]
        self._i += 1
        if verb == "ok":
            return Result.ok()
        if verb == "retry":
            return Result.failed("scripted retry")
        if verb == "permanent":
            return Result.failed("scripted permanent", permanent=True)
        raise AssertionError(f"unknown verb {verb!r}")


class UnconfiguredProvider:
    def send(self, message) -> Result:
        raise CapabilityUnconfigured("smtp is not configured for this tenant")


class CrashThenOkProvider:
    """First call raises an ordinary exception; then works."""

    def __init__(self):
        self._crashed = False

    def send(self, message) -> Result:
        if not self._crashed:
            self._crashed = True
            raise RuntimeError("connection reset")
        return Result.ok()


# --------------------------------------------------------------------------
class _MailDrainBase(TransactionTestCase):
    def setUp(self):
        self.providers = self.enterContext(override_capability({}))
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")

    def register(self, provider):
        self.providers["email.send"] = provider

    def enqueue(
        self,
        *,
        to_address="claimant@example.test",
        body_text="123456",
        expires_at=None,
        kind="claim.verification_code",
        tenant=None,
    ):
        # expires_at is required by audit.mail.enqueue; tests that don't care
        # about expiry get a deadline far enough out not to interfere.
        if expires_at is None:
            expires_at = timezone.now() + timedelta(hours=24)
        with transaction.atomic():
            return enqueue_message(
                tenant=tenant or self.tenant,
                kind=kind,
                to_address=to_address,
                subject="Your verification code",
                body_text=body_text,
                expires_at=expires_at,
            )


class ClaimTests(_MailDrainBase):
    def test_claim_ignores_rows_not_yet_due(self):
        message = self.enqueue()
        future = timezone.now() + HOUR
        OutboundMessage.all_tenants.filter(pk=message.pk).update(
            next_attempt_at=future
        )

        pks = mail_drain._claim(timezone.now(), 100)

        self.assertEqual(pks, [])

    def test_claim_pushes_visibility_deadline(self):
        message = self.enqueue()
        t0 = timezone.now()

        pks = mail_drain._claim(t0, 100)

        self.assertEqual(pks, [message.pk])
        message.refresh_from_db()
        self.assertEqual(
            message.next_attempt_at,
            t0 + timedelta(seconds=CLAIM_VISIBILITY_SECONDS),
        )


class ExpiryTests(_MailDrainBase):
    def test_expired_row_is_marked_expired_and_never_sent(self):
        provider = ScriptedProvider(["ok"])  # would succeed if ever called
        self.register(provider)
        deadline = timezone.now() + timedelta(minutes=5)
        message = self.enqueue(expires_at=deadline)

        stats = mail_drain_once(now=deadline + timedelta(seconds=1))

        self.assertEqual(stats.expired, 1)
        self.assertEqual(provider.seen_tenants, [])  # never attempted
        message.refresh_from_db()
        self.assertEqual(message.status, OutboundMessage.Status.EXPIRED)
        self.assertIsNone(message.body_text)

    def test_expiry_is_required_ruling_change(self):
        # OutboundMessage.expires_at is now non-null (ruling change): every
        # producer sets a deadline, and expiry -- not an attempt count --
        # bounds every row's lifetime. See audit.mail.enqueue's validation.
        message = self.enqueue(expires_at=timezone.now() + timedelta(hours=1))
        self.assertIsNotNone(message.expires_at)


class UnconfiguredTests(_MailDrainBase):
    def test_no_provider_registered_leaves_row_pending_no_attempt_consumed(self):
        message = self.enqueue()

        stats = mail_drain_once(now=timezone.now())

        self.assertEqual(stats.unconfigured, 1)
        message.refresh_from_db()
        self.assertEqual(message.status, OutboundMessage.Status.PENDING)
        self.assertEqual(message.attempt, 0)
        self.assertIsNotNone(message.body_text)  # not terminal, not nulled

    def test_capability_unconfigured_leaves_row_pending_no_attempt_consumed(self):
        self.register(UnconfiguredProvider())
        message = self.enqueue()

        stats = mail_drain_once(now=timezone.now())

        self.assertEqual(stats.unconfigured, 1)
        message.refresh_from_db()
        self.assertEqual(message.status, OutboundMessage.Status.PENDING)
        self.assertEqual(message.attempt, 0)

    def test_no_provider_registered_next_attempt_at_is_fixed_recheck(self):
        message = self.enqueue()
        t0 = timezone.now()

        mail_drain_once(now=t0)

        message.refresh_from_db()
        self.assertEqual(message.next_attempt_at, t0 + UNCONFIGURED_RECHECK)
        self.assertEqual(message.attempt, 0)

    def test_capability_unconfigured_next_attempt_at_is_fixed_recheck(self):
        self.register(UnconfiguredProvider())
        message = self.enqueue()
        t0 = timezone.now()

        mail_drain_once(now=t0)

        message.refresh_from_db()
        self.assertEqual(message.next_attempt_at, t0 + UNCONFIGURED_RECHECK)
        self.assertEqual(message.attempt, 0)

    def test_recheck_is_a_fixed_interval_not_backoff(self):
        # Two passes, each finding the row still unconfigured: the second
        # recheck is exactly one more UNCONFIGURED_RECHECK out, not a longer,
        # backoff()-shaped gap -- there is no attempt count driving it.
        message = self.enqueue()
        t0 = timezone.now()
        mail_drain_once(now=t0)
        message.refresh_from_db()
        first_recheck = message.next_attempt_at
        self.assertEqual(first_recheck, t0 + UNCONFIGURED_RECHECK)

        mail_drain_once(now=first_recheck)
        message.refresh_from_db()
        self.assertEqual(message.next_attempt_at, first_recheck + UNCONFIGURED_RECHECK)
        self.assertEqual(message.attempt, 0)

    def test_drain_warns_once_per_pass_not_once_per_row(self):
        self.enqueue()
        self.enqueue()
        self.enqueue()

        with self.assertLogs("osds.mail", level="WARNING") as captured:
            mail_drain_once(now=timezone.now())

        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelname, "WARNING")
        self.assertIn("3", captured.records[0].getMessage())


class DeliveryOutcomeTests(_MailDrainBase):
    def test_ok_marks_sent_and_stamps_sent_at(self):
        self.register(ScriptedProvider(["ok"]))
        message = self.enqueue()
        t0 = timezone.now()

        mail_drain_once(now=t0)

        message.refresh_from_db()
        self.assertEqual(message.status, OutboundMessage.Status.SENT)
        self.assertEqual(message.attempt, 1)
        self.assertEqual(message.sent_at, t0)

    def test_permanent_failure_dead_letters_immediately(self):
        self.register(ScriptedProvider(["permanent"]))
        message = self.enqueue()

        mail_drain_once(now=timezone.now())

        message.refresh_from_db()
        self.assertEqual(message.status, OutboundMessage.Status.DEAD)
        self.assertEqual(message.attempt, 1)
        self.assertEqual(message.last_error, "scripted permanent")

    def test_retryable_failure_stays_pending_with_backoff(self):
        self.register(ScriptedProvider(["retry"]))
        message = self.enqueue()
        t0 = timezone.now()

        mail_drain_once(now=t0)

        message.refresh_from_db()
        self.assertEqual(message.status, OutboundMessage.Status.PENDING)
        self.assertEqual(message.attempt, 1)
        self.assertGreater(message.next_attempt_at, t0)

    def test_retryable_failure_never_dead_letters_on_attempt_count_alone(self):
        # No MAX_ATTEMPTS here (unlike the outbox drain) -- expires_at is the
        # only bound on a mail message's lifetime (§4.3). Give it a deadline
        # well past the 50 hourly retries below, so expiry never intervenes.
        provider = ScriptedProvider(["retry"] * 50)
        self.register(provider)
        t = timezone.now()
        message = self.enqueue(expires_at=t + timedelta(hours=200))

        for _ in range(50):
            OutboundMessage.all_tenants.filter(pk=message.pk).update(
                next_attempt_at=t
            )
            with tenant_context(self.tenant):
                disposition = attempt_message(
                    OutboundMessage.all_tenants.get(pk=message.pk), now=t
                )
            self.assertEqual(disposition, "retried")
            t += HOUR

        message.refresh_from_db()
        self.assertEqual(message.status, OutboundMessage.Status.PENDING)
        self.assertEqual(message.attempt, 50)

    def test_an_ordinary_exception_from_the_provider_retries(self):
        self.register(CrashThenOkProvider())
        message = self.enqueue()

        mail_drain_once(now=timezone.now())
        message.refresh_from_db()
        self.assertEqual(message.status, OutboundMessage.Status.PENDING)
        self.assertEqual(message.attempt, 1)

        mail_drain_once(now=timezone.now() + HOUR)
        message.refresh_from_db()
        self.assertEqual(message.status, OutboundMessage.Status.SENT)


class RecordQueryCountTests(_MailDrainBase):
    """``_record`` writes ``first_attempted_at`` via ``Coalesce`` in the same
    statement as everything else -- one attempt, one query, not a fields
    update followed by a separate first-attempt check."""

    def test_sent_row_costs_exactly_one_update(self):
        message = self.enqueue()

        with self.assertNumQueries(1):
            applied = _record(
                message,
                now=timezone.now(),
                status=OutboundMessage.Status.SENT,
                attempt=1,
                sent=True,
            )

        self.assertTrue(applied)
        message.refresh_from_db()
        self.assertEqual(message.status, OutboundMessage.Status.SENT)

    def test_first_attempted_at_is_still_stamped_exactly_once(self):
        message = self.enqueue()
        t0 = timezone.now()
        _record(message, now=t0, status=OutboundMessage.Status.PENDING, attempt=1)
        message.refresh_from_db()
        self.assertEqual(message.first_attempted_at, t0)

        # A second attempt, later, must not move it.
        t1 = t0 + HOUR
        _record(message, now=t1, status=OutboundMessage.Status.PENDING, attempt=2)
        message.refresh_from_db()
        self.assertEqual(message.first_attempted_at, t0)  # unchanged
        self.assertEqual(message.last_attempted_at, t1)


class BodyAndAddressRetentionTests(_MailDrainBase):
    def test_body_text_nulled_to_none_on_sent(self):
        self.register(ScriptedProvider(["ok"]))
        message = self.enqueue()

        mail_drain_once(now=timezone.now())

        message.refresh_from_db()
        self.assertIsNone(message.body_text)

    def test_body_text_nulled_to_none_on_dead(self):
        self.register(ScriptedProvider(["permanent"]))
        message = self.enqueue()

        mail_drain_once(now=timezone.now())

        message.refresh_from_db()
        self.assertIsNone(message.body_text)

    def test_body_text_nulled_to_none_on_expired(self):
        self.register(ScriptedProvider(["ok"]))
        deadline = timezone.now() + timedelta(minutes=5)
        message = self.enqueue(expires_at=deadline)

        mail_drain_once(now=deadline + timedelta(seconds=1))

        message.refresh_from_db()
        self.assertIsNone(message.body_text)

    def test_body_text_kept_while_pending(self):
        self.register(ScriptedProvider(["retry"]))
        message = self.enqueue()

        mail_drain_once(now=timezone.now())

        message.refresh_from_db()
        self.assertEqual(message.body_text, "123456")

    def test_to_address_survives_every_terminal_state(self):
        self.register(ScriptedProvider(["ok"]))
        message = self.enqueue()

        mail_drain_once(now=timezone.now())

        message.refresh_from_db()
        self.assertEqual(message.status, OutboundMessage.Status.SENT)
        self.assertEqual(message.to_address, "claimant@example.test")


class TenantScopingTests(_MailDrainBase):
    def test_each_message_is_attempted_in_its_own_tenant_context(self):
        other = Tenant.objects.create(slug="beta", name="Beta")
        provider = ScriptedProvider(["ok", "ok"])
        self.register(provider)
        self.enqueue(tenant=self.tenant)
        self.enqueue(tenant=other)

        mail_drain_once(now=timezone.now())

        self.assertCountEqual(provider.seen_tenants, [self.tenant, other])

    def test_no_tenant_in_scope_outside_the_drain(self):
        from osds.tenancy import NoTenantInScope

        with self.assertRaises(NoTenantInScope):
            list(OutboundMessage.objects.all())


class StaleRecordTests(_MailDrainBase):
    def test_stale_record_after_hung_handler_is_discarded(self):
        """A handler that hangs past its claim deadline has its row
        re-claimed and finished by a later pass; when it finally records,
        the result lands on a moved target and is dropped."""
        # Two "ok" calls: one for the real pass that finishes the message,
        # one for the late-arriving stale handler -- its result is dropped,
        # so what it returns does not matter.
        provider = ScriptedProvider(["ok", "ok"])
        self.register(provider)
        message = self.enqueue()

        t0 = timezone.now()
        claimed = mail_drain._claim(t0, 100)
        self.assertEqual(claimed, [message.pk])
        slow = OutboundMessage.all_tenants.select_related("tenant").get(
            pk=message.pk
        )  # carries next_attempt_at == t0 + CLAIM_VISIBILITY_SECONDS

        # A later pass, past the deadline: re-claims and finishes it.
        t1 = t0 + timedelta(seconds=CLAIM_VISIBILITY_SECONDS + 10)
        mail_drain_once(now=t1)
        row = OutboundMessage.all_tenants.get(pk=message.pk)
        self.assertEqual(row.status, OutboundMessage.Status.SENT)
        self.assertEqual(row.attempt, 1)

        # The original slow attempt finally returns.
        with tenant_context(slow.tenant):
            disposition = attempt_message(slow, now=t0)

        self.assertEqual(disposition, "discarded")
        row = OutboundMessage.all_tenants.get(pk=message.pk)
        self.assertEqual(row.status, OutboundMessage.Status.SENT)  # kept
        self.assertEqual(row.attempt, 1)  # not re-counted


class NoEventOrCommandLogTests(_MailDrainBase):
    def test_sending_emits_no_outbox_event(self):
        from audit.models import OutboxEvent

        self.register(ScriptedProvider(["ok"]))
        self.enqueue()
        before = OutboxEvent.all_tenants.count()

        mail_drain_once(now=timezone.now())

        self.assertEqual(OutboxEvent.all_tenants.count(), before)

    def test_sending_writes_no_command_log(self):
        from audit.models import CommandLog

        self.register(ScriptedProvider(["ok"]))
        self.enqueue()
        before = CommandLog.objects.count()

        mail_drain_once(now=timezone.now())

        self.assertEqual(CommandLog.objects.count(), before)


class DrainSourceInvariantTests(SimpleTestCase):
    def test_mail_drain_imports_no_adapter_code(self):
        import ast
        import pathlib

        import audit.worker.mail_drain as module

        path = pathlib.Path(module.__file__)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name.split(".")[0], "adapters")
            elif isinstance(node, ast.ImportFrom):
                head = (node.module or "").split(".")[0]
                self.assertNotEqual(head, "adapters")
