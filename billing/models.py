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
from osds.ids import ent_id, tier_id


class Tier(models.Model):
    """A tenant-configured placement level (spec §4.2). An ordered list with a
    rank; rank 0 is the fallback. A tenant may define no rank-0 tier, which
    changes downgrade behaviour (spec §6.4)."""

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
            models.Index(fields=["listing"]),
        ]

    def __str__(self) -> str:
        return f"{self.public_id}:{self.status}"
