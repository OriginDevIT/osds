"""Tiers and entitlements.

Core owns entitlement state; adapters own money (CLAUDE.md invariant 2). There
is no code path that sets a listing's tier directly -- tier is resolved from
the entitlement record. Both models are tenant-scoped.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from osds.db import TenantScopedManager
from osds.ids import chk_id, ent_id, tier_id


class Tier(models.Model):
    """A tenant-configured placement level (spec §4.2). An ordered list with a
    rank; rank 0 is the fallback. A tenant may define no rank-0 tier, which
    changes downgrade behaviour (spec §6.4)."""

    class Interval(models.TextChoices):
        MONTH = "month", "Monthly"
        YEAR = "year", "Yearly"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="tiers"
    )
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=tier_id
    )
    key = models.SlugField(max_length=50)
    name = models.CharField(max_length=100)
    rank = models.PositiveSmallIntegerField()
    purchasable = models.BooleanField(default=False)
    uses_slot = models.BooleanField(default=False)
    # Pricing is populated in block 4; the columns exist now so the lifecycle
    # seam is built once. Integer minor units plus an ISO 4217 code.
    price_minor = models.PositiveIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    # How often a purchasable tier bills (decisions.md §4.11). Fixed terms are
    # tied to slot pools and arrive with them.
    interval = models.CharField(max_length=5, choices=Interval.choices, blank=True)
    # A card-up-front trial (spec §6.7); null means none.
    trial_days = models.PositiveSmallIntegerField(null=True, blank=True)
    perks = models.JSONField(default=dict, blank=True)
    badge_label = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "tiers"
        ordering = ["rank"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "key"], name="uniq_tier_tenant_key"
            ),
            models.UniqueConstraint(
                fields=["tenant", "rank"], name="uniq_tier_tenant_rank"
            ),
            # The fallback tier is the free one (spec §4.2).
            models.CheckConstraint(
                condition=~models.Q(rank=0, purchasable=True),
                name="tier_rank0_not_purchasable",
            ),
            # A tier that can be bought says what it costs and how often.
            models.CheckConstraint(
                condition=models.Q(purchasable=False)
                | (
                    models.Q(price_minor__gt=0)
                    & ~models.Q(currency="")
                    & ~models.Q(interval="")
                ),
                name="tier_purchasable_has_price",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.key}(rank {self.rank})"


class Entitlement(models.Model):
    """Tier and period state for one listing, as decided by core (spec §6.1).

    Every state transition (spec §6.3) gets a test from the day this model
    exists -- that table is where the system rots if it rots (decisions.md §3).
    """

    class Status(models.TextChoices):
        NONE = "none", "None"
        TRIALING = "trialing", "Trialing"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        GRACE = "grace", "Grace"
        EXPIRED = "expired", "Expired"
        CANCELED = "canceled", "Canceled"
        COMPED = "comped", "Comped"

    class BillingMode(models.TextChoices):
        RECURRING = "recurring", "Recurring"
        TERM = "term", "Fixed term"
        COMP = "comp", "Comp"
        NONE = "none", "None"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="entitlements"
    )
    listing = models.ForeignKey(
        "directory.Listing",
        on_delete=models.CASCADE,
        related_name="entitlements",
    )
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=ent_id
    )
    tier = models.ForeignKey(
        Tier, on_delete=models.PROTECT, related_name="entitlements"
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.NONE
    )
    billing_mode = models.CharField(
        max_length=12, choices=BillingMode.choices, default=BillingMode.NONE
    )
    term_days = models.PositiveSmallIntegerField(null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    dunning_started_at = models.DateTimeField(null=True, blank=True)
    grace_ends_at = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    # Dunning bookkeeping (spec §6.10 ``entitlement.dunning_started`` carries
    # ``attempt``, ``failure_code`` and ``dunning_ends_at``).
    dunning_ends_at = models.DateTimeField(null=True, blank=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    last_failure_code = models.CharField(max_length=100, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    canceled_by = models.CharField(max_length=20, blank=True)  # owner|operator|system
    # The T-10 renewal notice for a term entitlement is sent once a period.
    renewal_notified_at = models.DateTimeField(null=True, blank=True)
    # Set when expiry hid the listing because the tenant has no rank-0 tier
    # (spec §6.4); a repurchase unhides it only if this is set.
    hidden_by_expiry = models.BooleanField(default=False)

    comp_granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="comps_granted",
    )
    comp_reason = models.TextField(blank=True)
    comp_expires_at = models.DateTimeField(null=True, blank=True)

    # {adapter, external_id} -- written by the payment adapter, read by core.
    # Capability-neutral: core never branches on its contents.
    payment_ref = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "entitlements"
        indexes = [
            models.Index(fields=["tenant", "status"]),
        ]
        constraints = [
            # One entitlement per listing; history is the event log
            # (decisions.md §4.11).
            models.UniqueConstraint(fields=["listing"], name="uniq_entitlement_listing"),
        ]

    def __str__(self) -> str:
        return f"{self.public_id}:{self.status}"


class CheckoutAttempt(models.Model):
    """One checkout core started for a listing (decisions.md §4.11).

    Core decides the tier and the price when it creates this row, and hands the
    provider only what the provider needs; a completion that comes back must
    name an attempt core started, for this listing, so a signed event cannot
    buy a tier core never offered. ``external_ref`` is whatever the provider
    calls the session -- opaque to core.
    """

    class Status(models.TextChoices):
        STARTED = "started", "Started"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="checkout_attempts"
    )
    listing = models.ForeignKey(
        "directory.Listing", on_delete=models.CASCADE, related_name="checkout_attempts"
    )
    tier = models.ForeignKey(Tier, on_delete=models.PROTECT, related_name="checkout_attempts")
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=chk_id
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.STARTED)
    adapter_id = models.CharField(max_length=50, blank=True)
    external_ref = models.CharField(max_length=200, blank=True)
    # Who asked: the owner's user row. Not an operator.
    started_by = models.ForeignKey(
        "directory.DirectoryUser", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="checkout_attempts",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "checkout_attempts"
        indexes = [models.Index(fields=["tenant", "listing", "created_at"])]

    def __str__(self) -> str:
        return self.public_id


class PaymentReceipt(models.Model):
    """A payment report core has seen, keyed by the provider's event id
    (decisions.md §4.11). Inserted in the same transaction as the state change
    it caused, so a redelivery -- including two at once -- loses to the unique
    constraint and never applies twice.
    """

    class Outcome(models.TextChoices):
        APPLIED = "applied", "Applied"
        IGNORED = "ignored", "Ignored"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="payment_receipts"
    )
    adapter_id = models.CharField(max_length=50)
    external_event_id = models.CharField(max_length=200)
    kind = models.CharField(max_length=40)
    listing = models.ForeignKey(
        "directory.Listing", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="payment_receipts",
    )
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    # Why it was ignored (the state machine's refusal, an unknown checkout).
    reason = models.CharField(max_length=60, blank=True)
    received_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "payment_receipts"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "adapter_id", "external_event_id"],
                name="uniq_payment_receipt_event",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.adapter_id}:{self.external_event_id}"
