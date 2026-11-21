"""Probe a tenant's configured media storage: write, read, list and delete a
probe object through the same resolver the app uses. Exits non-zero on failure.
Prints the failing step and the provider's error, never a credential."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from directory.storage import StorageConfigError, check_storage, get_tenant_storage, tenant_storage_backend
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Check a tenant's media storage backend end to end."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Tenant slug; defaults to every active tenant.")

    def handle(self, *args, **options):
        tenants = Tenant.objects.all()
        if options["tenant"]:
            tenants = tenants.filter(slug=options["tenant"])
        failed = 0
        for tenant in tenants:
            try:
                check_storage(get_tenant_storage(tenant))
            except StorageConfigError as exc:
                failed += 1
                self.stderr.write(f"{tenant.slug}: FAILED ({tenant_storage_backend(tenant)}): {exc}")
            else:
                self.stdout.write(f"{tenant.slug}: ok ({tenant_storage_backend(tenant)})")
        if failed:
            raise CommandError(f"{failed} tenant(s) failed the storage check")
