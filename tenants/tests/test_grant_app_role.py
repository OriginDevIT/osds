"""`grant_app_role` gives the app role DML and no DDL, and is idempotent.

Needs a test database user that can CREATE ROLE (the superuser, as the suite
already requires). Roles are transactional in Postgres, so TestCase rolls the
test role back.
"""

from __future__ import annotations

import io

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase

from tenants.models import Tenant

ROLE = "osds_grant_test_app"


def _can(function: str, role: str, target: str, privilege: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {function}(%s, %s, %s)", [role, target, privilege])
        return cursor.fetchone()[0]


def _grant(role: str = ROLE) -> str:
    out = io.StringIO()
    call_command("grant_app_role", role=role, stdout=out, stderr=io.StringIO())
    return out.getvalue()


class GrantAppRoleTests(TestCase):
    def setUp(self):
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
            )
        self.table = Tenant._meta.db_table

    def test_role_can_read_and_write_rows(self):
        self.assertFalse(_can("has_table_privilege", ROLE, self.table, "SELECT"))
        _grant()
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            self.assertTrue(
                _can("has_table_privilege", ROLE, self.table, privilege), privilege
            )

    def test_role_can_use_sequences(self):
        _grant()
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [self.table])
            sequence = cursor.fetchone()[0]
        self.assertTrue(_can("has_sequence_privilege", ROLE, sequence, "USAGE"))

    def test_role_gets_no_ddl_and_no_truncate(self):
        _grant()
        self.assertFalse(_can("has_table_privilege", ROLE, self.table, "TRUNCATE"))
        self.assertFalse(_can("has_schema_privilege", ROLE, "public", "CREATE"))

    def test_tables_created_later_are_covered(self):
        _grant()
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE grant_test_later (id serial primary key)")
        self.assertTrue(
            _can("has_table_privilege", ROLE, "grant_test_later", "INSERT")
        )

    def test_is_idempotent(self):
        first, second = _grant(), _grant()
        self.assertEqual(first, second)

    def test_unknown_role_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "does not exist"):
            _grant("osds_no_such_role")

    def test_the_running_role_is_refused(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            owner = cursor.fetchone()[0]
        with self.assertRaisesMessage(CommandError, "different, least-privilege role"):
            _grant(owner)

    def test_empty_role_is_refused(self):
        with self.assertRaises(CommandError):
            _grant("  ")
