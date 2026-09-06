"""First-run state, derived from data (not a wizard cursor) so the wizard
resumes correctly after the browser is closed, on a different device, or on a
different app process.
"""

from __future__ import annotations

# Ordered wizard steps after "unlock". "done" is the confirmation screen.
STEP_ORDER = ["account", "directory", "domain", "storage", "smtp", "claims", "done"]


def setup_complete() -> bool:
    from tenants.models import InstallSetup

    row = InstallSetup.load()
    return row is not None and row.completed_at is not None


def next_step() -> str:
    """The first step whose work has not been done yet."""
    from tenants.models import Operator, Tenant

    if not Operator.objects.exists():
        return "account"

    tenant = Tenant.objects.order_by("id").first()
    if tenant is None:
        return "directory"
    if not tenant.primary_domain:
        return "domain"

    settings_doc = tenant.settings or {}
    if "storage" not in settings_doc:
        return "storage"
    if "smtp" not in settings_doc:
        return "smtp"
    if "claim_verification" not in settings_doc:
        return "claims"
    return "done"
