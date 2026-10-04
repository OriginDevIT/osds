"""Shared fixtures for the lead-capture tests (decisions.md §4.10).

``create_lead`` refuses to run inside an open transaction, so everything that
calls it is a TransactionTestCase.
"""

from __future__ import annotations

from django.test import TransactionTestCase
from django.utils import timezone

from audit.tests.window_clock import pinned_windows
from directory import leads
from directory.models import Category, DirectoryUser, Listing, ListingType
from osds.tenancy import tenant_context
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant

HOST = "acme.test"
LISTING_EMAIL = "info@hoffmanplumbing.example"
GOOD_MESSAGE = "Burst pipe under the sink, need someone today."
GRANTED = {"contact_by_business": {"granted": True}}
Role = StaffMembership.Role


class LeadBase(TransactionTestCase):
    def setUp(self):
        # Every flood lands in one window, however slow the runner (#245).
        self.enterContext(pinned_windows())
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain=HOST,
            domain_verified_at=timezone.now(), settings={"leads": {"enabled": True}},
        )
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="Business",
            label_plural="Businesses", path_segment="businesses",
        )
        self.cat = Category.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="plumbers", name="Plumbers"
        )
        self.n = 0
        self.listing = self.make_listing("hoffman-plumbing")

    # -- fixtures ---------------------------------------------------------
    def make_listing(self, slug, *, owner=None, **kw):
        defaults = dict(
            tenant=self.tenant, listing_type=self.lt, slug=slug, name=slug.title(),
            visibility=Listing.Visibility.PUBLISHED, email=LISTING_EMAIL, owner=owner,
        )
        defaults.update(kw)
        listing = Listing.all_tenants.create(**defaults)
        with tenant_context(self.tenant):
            listing.categories.set([self.cat])
        return listing

    def make_owner(self, email="owner@hoffmanplumbing.example"):
        return DirectoryUser.all_tenants.create(
            tenant=self.tenant, email=email, name="Dana Hoffman"
        )

    def op(self, role, *, tenant=None, active=True):
        self.n += 1
        operator = Operator.objects.create_user(
            email=f"op{self.n}@acme.test", password="pw"
        )
        operator.is_active = active
        operator.save(update_fields=["is_active"])
        StaffMembership.objects.create(
            operator=operator, tenant=tenant or self.tenant, role=role,
            status=StaffMembership.Status.ACTIVE,
        )
        return operator

    def create(self, **over):
        self.n += 1
        kwargs = dict(
            listing=self.listing,
            kind="contact_form",
            contact={
                "name": "Priya R.",
                "email": f"priya{self.n}@example.test",
                "phone_e164": "+13125550188",
            },
            message=f"{GOOD_MESSAGE} ({self.n})",
            consent=GRANTED,
            source_page="/plumbers/hoffman-plumbing",
            ip="198.51.100.7",
        )
        kwargs.update(over)
        with tenant_context(self.tenant):
            return leads.create_lead(self.tenant, **kwargs)

    def fresh(self, obj):
        return type(obj).all_tenants.get(pk=obj.pk)
