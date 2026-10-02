"""Writing the access log (spec §11.2): who viewed or exported what.

Two-year retention, pruned by the worker's ``access_log_retention`` job. The
first writer is the admin lead views (decisions.md §4.10). Nothing here records
a login: authentication writes to no log (decisions.md §4).
"""

from __future__ import annotations

from audit.models import AccessLog
from osds.client_ip import storable_ip


def record_view(
    *,
    tenant,
    operator,
    role: int,
    resource_type: str,
    resource_id: str,
    ip: "str | None",
    user_agent: str = "",
    extra: "dict | None" = None,
) -> AccessLog:
    """One row for one operator viewing one record. ``role`` is the rank the
    view was served at, so a later reader can tell a masked view from a full
    one without the operator's membership as it was then."""
    return AccessLog.all_tenants.create(
        tenant=tenant,
        actor={"type": "admin" if role >= 4 else "staff", "id": operator.public_id},
        action=AccessLog.Action.VIEWED,
        resource_type=resource_type,
        resource_id=resource_id,
        ip=storable_ip(ip),
        user_agent=(user_agent or "")[:400],
        extra=extra or {},
    )
