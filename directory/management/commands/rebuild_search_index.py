"""Recompute stale listing search vectors.

Default: drain pending SearchReindexJob markers (written by
update_listing_type / update_category) and recompute the listings each one
covers. This is the same drain the worker's ``search_reindex`` tick job runs
(``directory.search.drain_reindex_markers``), run to completion by hand.
``--all`` recomputes every listing regardless of markers, for recovery or after
a tenant's ``search_config`` changes -- nothing writes a whole-tenant marker for
that, so it is not automatic.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from directory.search import drain_reindex_markers, recompute_search_vector, reindex_queryset
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
        if options["all"]:
            for tenant in tenants:
                with tenant_context(tenant):
                    total += self._recompute(reindex_queryset())
        else:
            for tenant in tenants:
                while True:
                    result = drain_reindex_markers(now=timezone.now(), tenant=tenant)
                    total += result.listings
                    if not result.more:
                        break
        self.stdout.write(f"Recomputed {total} listing search vector(s).")

    @staticmethod
    def _recompute(queryset) -> int:
        count = 0
        for listing in queryset.iterator(chunk_size=200):  # prefetch_related needs it
            recompute_search_vector(listing)
            count += 1
        return count
