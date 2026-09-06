"""Recompute stale listing search vectors.

Default: drain pending SearchReindexJob markers (written by
update_listing_type / update_category) and recompute the listings each one
covers. ``--all`` recomputes every listing regardless of markers, for
recovery or after a tenant's ``search_config`` changes. The worker tick will
run the default path once it exists (ruling 7).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from directory.models import Listing, SearchReindexJob
from directory.search import recompute_search_vector
from osds.tenancy import tenant_context
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Recompute stale listing search vectors."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Limit to one tenant slug.")
        parser.add_argument(
            "--all",
            action="store_true",
            help="Recompute every listing, ignoring markers.",
        )

    def handle(self, *args, **options):
        tenants = Tenant.objects.all().order_by("id")
        if options["tenant"]:
            tenants = tenants.filter(slug=options["tenant"])

        total = 0
        for tenant in tenants:
            with tenant_context(tenant):
                total += self._reindex_tenant(tenant, force_all=options["all"])
        self.stdout.write(f"Recomputed {total} listing search vector(s).")

    def _reindex_tenant(self, tenant, *, force_all: bool) -> int:
        if force_all:
            return self._recompute(self._base_qs())

        jobs = SearchReindexJob.objects.filter(tenant=tenant, done_at__isnull=True)
        pending = list(jobs)
        if not pending:
            return 0

        listing_ids: set[int] = set()
        for job in pending:
            listing_ids.update(self._listings_for(job).values_list("id", flat=True))

        count = self._recompute(self._base_qs().filter(id__in=listing_ids))
        jobs.update(done_at=timezone.now())
        return count

    @staticmethod
    def _base_qs():
        return Listing.objects.select_related("listing_type").prefetch_related(
            "categories"
        )

    @staticmethod
    def _listings_for(job):
        if job.scope == SearchReindexJob.Scope.LISTING_TYPE:
            return Listing.objects.filter(listing_type__public_id=job.scope_ref)
        if job.scope == SearchReindexJob.Scope.CATEGORY:
            return Listing.objects.filter(categories__public_id=job.scope_ref)
        return Listing.objects.all()  # tenant-wide

    @staticmethod
    def _recompute(queryset) -> int:
        count = 0
        for listing in queryset.iterator(chunk_size=200):  # prefetch_related needs it
            recompute_search_vector(listing)
            count += 1
        return count
