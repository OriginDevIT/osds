"""Copy a tenant's stored files to another storage backend, then switch to it.

    copy_storage --tenant SLUG --dry-run
    copy_storage --tenant SLUG --target-file target.json --operator EMAIL

The target file is JSON: the backend's settings (``backend``, ``bucket``,
``region``, ...) plus a ``secrets`` object holding its credentials, e.g.
``{"backend": "s3", "bucket": "b", "region": "r", "secrets": {"access_key":
"...", "secret_key": "..."}}``. Credentials are read from that file on each run;
they are never printed, logged or stored with the copy records.

Re-running resumes: files already copied and verified are skipped. The backend
setting switches only when every file is verified. Source files are never
deleted; remove them separately once the new backend is confirmed.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from directory import storage_copy
from directory.storage import StorageConfigError
from tenants.models import Operator, Tenant


class Command(BaseCommand):
    help = "Copy a tenant's media and import files to another storage backend."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant slug.")
        parser.add_argument("--dry-run", action="store_true", help="List what would be copied; change nothing.")
        parser.add_argument("--target-file", help="JSON file with the target backend settings and secrets.")
        parser.add_argument("--operator", help="Email of the operator the settings change is recorded against.")

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(slug=options["tenant"])
        except Tenant.DoesNotExist:
            raise CommandError(f"no tenant with slug {options['tenant']!r}")

        if options["dry_run"]:
            return self._dry_run(tenant)

        if not options["target_file"]:
            raise CommandError("--target-file is required (or use --dry-run)")
        if not options["operator"]:
            raise CommandError("--operator is required: the backend switch is recorded against an operator")
        try:
            operator = Operator.objects.get(email=options["operator"].strip().lower(), is_active=True)
        except Operator.DoesNotExist:
            raise CommandError("no active operator with that email")
        cfg, secrets = self._read_target(options["target_file"])

        try:
            result = storage_copy.run_copy(tenant, cfg, secrets, operator=operator)
        except (storage_copy.CopyRefused, StorageConfigError) as exc:
            raise CommandError(str(exc))

        self.stdout.write(f"{tenant.slug}: copy {result.copy.public_id}: {result.copied} file(s) copied")
        for key in result.already_present:
            self.stdout.write(f"  already on target, identical, not rewritten: {key}")
        if result.failures:
            lines = "\n".join(f"  {f.key}: {f.error}" for f in result.failures)
            raise CommandError(
                f"{len(result.failures)} file(s) failed; the tenant stays on its current backend. "
                f"Fix the cause and re-run to resume.\n{lines}"
            )
        if not result.switched:
            raise CommandError("the copy did not settle (files kept arriving); re-run to continue")
        self.stdout.write(f"{tenant.slug}: switched to {result.copy.target_backend}. Source files were left in place.")

    def _dry_run(self, tenant):
        try:
            plan = storage_copy.plan_copy(tenant)
        except StorageConfigError as exc:
            raise CommandError(str(exc))
        for f in plan.files:
            self.stdout.write(f"  {f.kind:6} {f.size:>12}  {f.key}")
        for key in plan.missing:
            self.stdout.write(f"  MISSING at source: {key}")
        self.stdout.write(
            f"{tenant.slug}: would copy {plan.file_count} file(s), {plan.total_bytes} bytes "
            f"({len(plan.missing)} missing at source). Nothing was changed."
        )

    def _read_target(self, path: str) -> "tuple[dict, dict]":
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            raise CommandError(f"cannot read the target file: {type(exc).__name__}")
        if not isinstance(data, dict) or not data.get("backend"):
            raise CommandError("the target file must be a JSON object with a backend")
        secrets = data.pop("secrets", None) or {}
        if not isinstance(secrets, dict):
            raise CommandError("the target file's secrets must be an object")
        return data, {str(k): str(v) for k, v in secrets.items()}
