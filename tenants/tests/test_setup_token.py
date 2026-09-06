"""`ensure_setup_token` is idempotent and mints exactly one token."""

from __future__ import annotations

import hashlib
import io

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from tenants.models import InstallSetup


def _run() -> str:
    out = io.StringIO()
    call_command("ensure_setup_token", stdout=out)
    return out.getvalue()


class EnsureSetupTokenTests(TestCase):
    def test_first_run_mints_and_prints_a_token(self):
        output = _run()
        row = InstallSetup.load()
        self.assertIsNotNone(row)
        self.assertEqual(len(row.token_hash), 64)
        self.assertIsNone(row.completed_at)
        # the printed token hashes to the stored hash
        printed = [
            line.split("setup token:")[1].strip()
            for line in output.splitlines()
            if "setup token:" in line
        ]
        self.assertEqual(len(printed), 1)
        self.assertEqual(
            hashlib.sha256(printed[0].encode()).hexdigest(), row.token_hash
        )

    def test_second_run_is_a_no_op(self):
        _run()
        first = InstallSetup.load().token_hash
        output = _run()
        self.assertEqual(InstallSetup.load().token_hash, first)
        self.assertEqual(InstallSetup.objects.count(), 1)
        self.assertNotIn("setup token:", output)

    def test_no_op_after_setup_is_complete(self):
        _run()
        row = InstallSetup.load()
        row.completed_at = timezone.now()
        row.save()
        output = _run()
        self.assertIn("already complete", output)
        self.assertEqual(InstallSetup.objects.count(), 1)

    def test_singleton_pk(self):
        _run()
        self.assertEqual(InstallSetup.load().pk, 1)
