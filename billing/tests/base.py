"""Shared fixtures for the billing tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import TestCase, TransactionTestCase

from audit.models import OutboxEvent
from billing import entitlements
from billing.models import Entitlement, Tier
from directory.models import Listing, ListingType
from osds.tenancy import tenant_context
from tenants.models import Operator, StaffMembership, Tenant

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=dt_timezone.utc)
DAY = timedelta(days=1)
Role = StaffMembership.Role


class BillingFixtures:
    """Mixed into a TestCase or TransactionTestCase. Builds a tenant with a
    free (rank 0), a verified (rank 1) and a featured (rank 2) tier and one
    published listing."""

    with_rank0 = True

    def make_world(self):
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain="acme.test",
            domain_verified_at=T0,
        )
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="B",
            label_plural="Bs", path_segment="businesses",
        )
        self.free = (
            Tier.all_tenants.create(tenant=self.tenant, key="free", name="Free", rank=0)
            if self.with_rank0 else None
        )
        self.verified = Tier.all_tenants.create(
            tenant=self.tenant, key="verified", name="Verified", rank=1,
            purchasable=True, price_minor=900, currency="USD", interval="month",
            badge_label="Verified",
        )
        self.featured = Tier.all_tenants.create(
            tenant=self.tenant, key="featured", name="Featured", rank=2,
            purchasable=True, price_minor=2900, currency="USD", interval="month",
            trial_days=7, badge_label="Featured",
        )
        self.listing = self.make_listing("acme-co", "Acme Co")
        self._ops = 0

    def make_listing(self, slug, name):
        return Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug=slug, name=name,
            visibility=Listing.Visibility.PUBLISHED,
        )

    def operator(self, role, tenant=None):
        self._ops += 1
        op = Operator.objects.create_user(email=f"op{self._ops}@acme.test", password="pw")
        StaffMembership.objects.create(
            operator=op, tenant=tenant or self.tenant, role=role,
            status=StaffMembership.Status.ACTIVE,
        )
        return op

    # -- driving the machine --
    def apply(self, trigger, *, listing=None, now=T0, **params):
        listing = listing or self.listing
        with tenant_context(self.tenant):
            return entitlements.apply_trigger(self.tenant, listing, trigger, now=now, **params)

    def ent(self, listing=None) -> Entitlement:
        return Entitlement.all_tenants.get(listing=listing or self.listing)

    def fresh(self, listing=None) -> Listing:
        return Listing.all_tenants.get(pk=(listing or self.listing).pk)

    def events(self, *types, since=0):
        qs = OutboxEvent.all_tenants.filter(tenant=self.tenant, id__gt=since).order_by("id")
        if types:
            qs = qs.filter(type__in=types)
        return qs

    def last_event_id(self) -> int:
        last = OutboxEvent.all_tenants.order_by("id").last()
        return last.id if last else 0


class BillingTestCase(BillingFixtures, TestCase):
    def setUp(self):
        self.make_world()


class BillingTransactionTestCase(BillingFixtures, TransactionTestCase):
    def setUp(self):
        self.make_world()
