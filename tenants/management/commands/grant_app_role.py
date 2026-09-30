"""Grant the least-privilege application role what it needs, and nothing else.

Migrations run as the database owner (decisions.md section 4), so every table
is owned by the owner and the role the app connects as can touch none of them.
The container entrypoint runs this command as the owner after ``migrate``:

* ``USAGE`` on schema ``public``;
* ``SELECT, INSERT, UPDATE, DELETE`` on every existing table;
* ``USAGE, SELECT`` on every existing sequence;
* the same two grants as default privileges, so tables a later migration
  creates are reachable without another run.

No ``CREATE``, no ``TRUNCATE``, no DDL of any kind: a compromised app process
can read and write rows and cannot reshape the schema. Idempotent, and safe to
run on every start. It grants to a role that already exists; it never creates
one and never handles a password.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from psycopg import sql

_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"
_SEQUENCE_PRIVILEGES = "USAGE, SELECT"


class Command(BaseCommand):
    help = "Grant the application role DML on every table. Run as the database owner."

    def add_arguments(self, parser):
        parser.add_argument(
            "--role", required=True, help="The role the app connects as."
        )

    def handle(self, *args, role: str, **options):
        role = role.strip()
        if not role:
            raise CommandError("--role must not be empty.")

        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            owner = cursor.fetchone()[0]
            if role == owner:
                raise CommandError(
                    f"{role!r} is the role running this command. The app must "
                    "connect as a different, least-privilege role."
                )
            cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = %s", [role])
            found = cursor.fetchone()
            if found is None:
                raise CommandError(f"role {role!r} does not exist.")
            if found[0]:
                self.stderr.write(
                    f"warning: {role!r} is a superuser; these grants restrict nothing."
                )

            ident = sql.Identifier(role)
            statements = [
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident),
                sql.SQL(f"GRANT {_TABLE_PRIVILEGES} ON ALL TABLES IN SCHEMA public TO {{}}").format(ident),
                sql.SQL(f"GRANT {_SEQUENCE_PRIVILEGES} ON ALL SEQUENCES IN SCHEMA public TO {{}}").format(ident),
                sql.SQL(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT {_TABLE_PRIVILEGES} ON TABLES TO {{}}"
                ).format(ident),
                sql.SQL(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT {_SEQUENCE_PRIVILEGES} ON SEQUENCES TO {{}}"
                ).format(ident),
            ]
            for statement in statements:
                cursor.execute(statement)

        self.stdout.write(f"Granted application privileges on schema public to {role!r}.")
