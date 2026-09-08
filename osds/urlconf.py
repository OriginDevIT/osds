"""The three per-host URLconfs, named once.

``TenantResolutionMiddleware`` assigns ``request.urlconf`` from these on every
request; the root URLconf (``osds.urls``) stays empty on purpose, so nothing
resolves until the middleware has classified the host.

Non-request code (management commands, the worker, migrations) MUST NOT
``reverse()`` a route against one of these. ``reverse()`` with no request on
the stack falls back to the empty root URLconf and raises ``NoReverseMatch``;
and the public URL shape is dynamic (the ``path_segment`` prefix appears only
once a tenant has more than one listing type), so ``reverse()`` against the
``public_dispatch`` catch-all does no routing work anyway. Build URL strings
with ``directory.routing`` instead -- see ``docs/decisions.md`` §4.1.
"""

from __future__ import annotations

TENANT_URLCONF = "osds.urls_tenant"
CONSOLE_URLCONF = "osds.urls_console"
SETUP_URLCONF = "osds.urls_setup"
