"""Idempotently mint the first-run setup token.

Called from the container entrypoint on every start (not from
``AppConfig.ready()`` -- that runs inside ``manage.py`` invocations,
migrations and the test runner too). Safe to run repeatedly: the token is
minted once and never rotated here.
"""

from __future__ import annotations

import hashlib
import secrets as pysecrets

from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction

from tenants.models import InstallSetup


class Command(BaseCommand):
    help = "Mint the first-run setup token if it does not exist yet."

    def handle(self, *args, **options):
        existing = InstallSetup.load()
        if existing is not None:
            if existing.completed_at is not None:
                self.stdout.write("First-run setup is already complete.")
            else:
                self.stdout.write("Setup token already present; not re-minting.")
            return

        token = pysecrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        try:
            with transaction.atomic():
                InstallSetup.objects.create(token_hash=digest)
        except IntegrityError:
            # Another container won the race between load() and create().
            self.stdout.write("Setup token already present; not re-minting.")
            return

        self.stdout.write("")
        self.stdout.write("=" * 64)
        self.stdout.write(f"  OSDS first-run setup token:  {token}")
        self.stdout.write("  Open the site in a browser and paste this to begin.")
        self.stdout.write("=" * 64)
        self.stdout.write("")
