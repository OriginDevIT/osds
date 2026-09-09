"""The event outbox and the three logs (spec §11).

- ``OutboxEvent`` -- the Postgres outbox / event log, written in the same
  transaction as the state change it records. ``status`` tracks *fan-out*, not
  delivery: ``pending`` until the worker creates one ``OutboxDelivery`` per
  subscribed adapter, then ``dispatched``. Per-adapter delivery progress --
  attempts, backoff, dead-lettering -- lives on ``OutboxDelivery``. ``tenant``
  is NOT NULL here; the serialised envelope still omits the tenant block for
  ``tenant.*`` types (``audit.envelope.to_wire``).
- ``OutboxDelivery`` -- one row per ``(event, adapter)``, carrying the retry
  state machine. ``tenant`` is nullable (a ``tenant.*`` event has no tenant
  block, spec §8) and ``subject`` is denormalised off the event so the
  per-subject head-of-line check never has to join ``outbox_events``. Managers
  mirror ``OutboxEvent`` -- scoped default plus ``all_tenants``; the worker
  drains it through ``all_tenants`` and enters each delivery's tenant
  explicitly. Not the §11.2 ``command_log`` exception.
- ``CommandLog`` -- every command attempted, including rejected and blocked.
  Written *outside* the command transaction (spec §11.2), so ``tenant`` is
  nullable: a malformed command may name no resolvable tenant. Plain manager,
  allowlisted in ``tenants/tests/test_scoped_manager.py``.
- ``AccessLog`` -- who viewed or exported what. Console and superadmin access
  has no tenant, so ``tenant`` is nullable. Plain manager, allowlisted.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from osds.db import TenantScopedManager
from osds.ids import new_ulid


class OutboxEvent(models.Model):
    """An emitted event. ``status`` is about fan-out, not delivery: it goes
    ``pending`` -> ``dispatched`` once the worker has written an
    ``OutboxDelivery`` row for every subscribed adapter (zero or more). Whether
    each adapter actually received it -- and how many attempts that took -- is
    on the ``OutboxDelivery`` rows, never here.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DISPATCHED = "dispatched", "Dispatched"
        DELIVERED = "delivered", "Delivered"
        DEAD = "dead", "Dead-lettered"

    class ActorType(models.TextChoices):
        VISITOR = "visitor", "Visitor"
        OWNER = "owner", "Owner"
        STAFF = "staff", "Staff"
        ADMIN = "admin", "Admin"
        SYSTEM = "system", "System"
        AGENT = "agent", "Agent"
        ADAPTER = "adapter", "Adapter"

    # Envelope id and idempotency key -- a bare 26-char ULID, no prefix.
    event_id = models.CharField(
        max_length=26, unique=True, editable=False, default=new_ulid
    )
    # Must appear in audit.events.ALL_EVENT_TYPES (test-enforced once the
    # service layer emits).
    type = models.CharField(max_length=60)
    version = models.PositiveSmallIntegerField(default=1)
    occurred_at = models.DateTimeField(default=timezone.now)
    subject = models.CharField(max_length=40)

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.PROTECT, related_name="events"
    )

    actor = models.JSONField(default=dict, blank=True)
    # Originating adapter id -- loop guard. Empty means core-originated.
    origin = models.CharField(max_length=100, blank=True)
    trace_id = models.CharField(max_length=26, blank=True)
    # Nulled at 90 days (spec §11.2).
    data = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    payload_nulled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    # Set when fan-out completes -- OutboxDelivery rows now exist for every
    # subscriber (or there were none). Not "delivered".
    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "outbox_events"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["status", "id"]),
            models.Index(fields=["subject"]),
            models.Index(fields=["type"]),
        ]

    def __str__(self) -> str:
        return f"{self.type}:{self.event_id}"


class OutboxDelivery(models.Model):
    """One event's delivery to one adapter. Created by the worker on fan-out;
    it then carries the retry state machine (spec §8.2): exponential jittered
    backoff, 12 attempts, then ``dead`` in the tenant DLQ with the envelope
    still reachable through ``event``.

    Managers mirror ``OutboxEvent``: the scoped default plus ``all_tenants``
    for the drain. This is *not* the §11.2 ``command_log`` exception -- a
    nullable ``tenant`` is not a licence for a plain manager, and
    ``OutboxEvent`` already carries null-tenant-adjacent ``tenant.*`` rows
    under the scoped default. The drain reaches across tenants through
    ``all_tenants`` and enters each delivery's tenant explicitly.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DELIVERED = "delivered", "Delivered"
        DEAD = "dead", "Dead-lettered"

    event = models.ForeignKey(
        OutboxEvent, on_delete=models.PROTECT, related_name="deliveries"
    )
    # Nullable: a tenant.* event carries no tenant block (spec §8), and
    # tenant.created is emitted on every tenant creation.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    # Denormalised from ``event.subject``: the per-subject head-of-line
    # NOT EXISTS runs on every claim and must not join ``outbox_events``.
    subject = models.CharField(max_length=40)
    adapter_id = models.CharField(max_length=100)

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    # Count of *completed* attempts. A crash mid-attempt is not an attempt.
    attempt = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    first_attempted_at = models.DateTimeField(null=True, blank=True)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "outbox_deliveries"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "adapter_id"],
                name="uniq_delivery_event_adapter",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"]),
            models.Index(fields=["adapter_id", "subject", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.adapter_id}:{self.status}(attempt {self.attempt})"


class CommandLog(models.Model):
    class Outcome(models.TextChoices):
        APPLIED = "applied", "Applied"
        REJECTED = "rejected", "Rejected"
        BLOCKED = "blocked", "Blocked"

    command = models.CharField(max_length=60)
    idempotency_key = models.CharField(max_length=200, null=True, blank=True)
    # §11.2 bounded exception: nullable so an attempt that never resolved a
    # tenant still leaves a trace.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="commands",
    )
    adapter_id = models.CharField(max_length=100, blank=True)
    actor = models.JSONField(default=dict, blank=True)
    trace_id = models.CharField(max_length=26, blank=True)
    payload = models.JSONField(null=True, blank=True)  # nulled at 90 days

    # Written before the command transaction opens.
    received_at = models.DateTimeField(default=timezone.now, editable=False)
    # Null means the command threw mid-apply -- that is the record, not a gap.
    outcome = models.CharField(
        max_length=10, choices=Outcome.choices, null=True, blank=True
    )
    result_event_id = models.CharField(max_length=26, blank=True)
    problem = models.JSONField(null=True, blank=True)
    # Written after the transaction settles. A concluded row is never rewritten.
    concluded_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager()

    class Meta:
        db_table = "command_log"
        indexes = [
            models.Index(fields=["idempotency_key"]),
            models.Index(fields=["received_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.command}:{self.outcome or 'pending'}"


class AccessLog(models.Model):
    class Action(models.TextChoices):
        VIEWED = "viewed", "Viewed"
        EXPORTED = "exported", "Exported"

    tenant = models.ForeignKey(
        "tenants.Tenant",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="access_events",
    )
    actor = models.JSONField(default=dict, blank=True)
    action = models.CharField(max_length=20, choices=Action.choices)
    resource_type = models.CharField(max_length=60)
    resource_id = models.CharField(max_length=64, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)
    extra = models.JSONField(default=dict, blank=True)

    objects = models.Manager()

    class Meta:
        db_table = "access_log"
        indexes = [
            models.Index(fields=["occurred_at"]),
            models.Index(fields=["resource_type", "resource_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.action}:{self.resource_type}"
