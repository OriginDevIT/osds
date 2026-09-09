"""A raw psycopg connection carrying the app's credentials, held outside
Django's connection lifecycle.

Two things in the worker must outlive a single drain pass: the ``LISTEN`` on
``osds_outbox`` and the single-instance advisory lock. Django recycles its own
connection between passes (``close_old_connections``), wraps it in the drain's
short transactions, and reconnects it on any error -- every one of those would
silently drop a ``LISTEN`` or release a session lock. These connections are
ours, opened once and closed only at shutdown.

The credentials come from ``get_connection_params()`` so a future
``OPTIONS``/service-file config is honoured; the Django-specific keys it adds
for ORM cursors are stripped.
"""

from __future__ import annotations

import psycopg
from django.db import connection as _orm_connection

_DJANGO_ONLY_KEYS = ("cursor_factory", "context", "prepare_threshold")


def raw_connection() -> "psycopg.Connection":
    """Open an autocommit psycopg connection to the same database Django uses.

    Autocommit because ``LISTEN`` and the session advisory lock must not sit
    inside a transaction.
    """
    params = _orm_connection.get_connection_params()
    for key in _DJANGO_ONLY_KEYS:
        params.pop(key, None)
    conn = psycopg.connect(**params)
    conn.autocommit = True
    return conn
