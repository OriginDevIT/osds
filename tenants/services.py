"""Service layer for principals and tenancy.

Views, management commands and (later) adapters call these; nothing else writes
these models. Each function runs in one transaction and writes any outbox row
in that same transaction.
"""

from __future__ import annotations

import secrets as pysecrets

from django.db import transaction
from django.utils import timezone

from audit import events
from audit.outbox import emit
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant


def _role_key(role: int) -> str:
    return StaffMembership.Role(role).name.lower()


@transaction.atomic
def create_superadmin(*, email: str, password: str, name: str = "") -> Operator:
    """The first-run superadmin. No event -- operator creation emits nothing,
    and superadmin status is command-log-only (spec §4.4)."""
    return Operator.objects.create_superuser(
        email=email, password=password, name=name
    )


@transaction.atomic
def create_tenant(*, name: str, slug: str, mode: str, created_by: Operator) -> Tenant:
    tenant = Tenant.objects.create(
        name=name, slug=slug, mode=mode, created_by=created_by
    )
    emit(
        events.TENANT_CREATED,
        subject=tenant.public_id,
        tenant=tenant,
        actor={"type": "admin", "id": created_by.public_id},
        data={
            "slug": tenant.slug,
            "mode": tenant.mode,
            "created_by": created_by.public_id,
        },
    )
    return tenant


@transaction.atomic
def add_bootstrap_membership(
    *, operator: Operator, tenant: Tenant, role: int = StaffMembership.Role.ADMIN
) -> StaffMembership:
    """The superadmin's own membership on the first tenant. Minted together with
    the operator's first directory, so it is active immediately: emits
    staff.invited then staff.accepted (spec §4.4)."""
    membership = StaffMembership.objects.create(
        operator=operator,
        tenant=tenant,
        role=role,
        status=StaffMembership.Status.ACTIVE,
        invited_by=operator,
        accepted_at=timezone.now(),
    )
    envelope = {
        "subject": operator.public_id,
        "tenant": tenant,
        "actor": {"type": "admin", "id": operator.public_id},
    }
    member = {
        "operator_id": operator.public_id,
        "role": _role_key(role),
        "status": "active",
    }
    emit(
        events.STAFF_INVITED,
        data={
            "membership": member,
            "operator": {"email": operator.email, "existing": True},
            "granted_by": operator.public_id,
        },
        **envelope,
    )
    emit(events.STAFF_ACCEPTED, data={"membership": member}, **envelope)
    return membership


@transaction.atomic
def set_tenant_domain(*, tenant: Tenant, domain: str, changed_by: Operator) -> Tenant:
    domain = domain.strip().rstrip(".").lower()
    had_domain = bool(tenant.primary_domain)
    tenant.primary_domain = domain
    tenant.domain_verified_at = None
    if not tenant.settings.get("domain_challenge"):
        tenant.settings["domain_challenge"] = pysecrets.token_urlsafe(24)
    tenant.save(
        update_fields=["primary_domain", "domain_verified_at", "settings"]
    )
    emit(
        events.TENANT_SETTINGS_CHANGED,
        subject=tenant.public_id,
        tenant=tenant,
        actor={"type": "admin", "id": changed_by.public_id},
        data={
            "changes": [
                {
                    "op": "replace" if had_domain else "add",
                    "path": "/primary_domain",
                    "value": domain,
                }
            ],
            "changed_by": changed_by.public_id,
        },
    )
    return tenant


@transaction.atomic
def update_tenant_settings(
    *, tenant: Tenant, changes: dict, changed_by: Operator
) -> Tenant:
    patch = []
    for key, value in changes.items():
        patch.append(
            {
                "op": "replace" if key in tenant.settings else "add",
                "path": f"/{key}",
                "value": value,
            }
        )
        tenant.settings[key] = value
    tenant.save(update_fields=["settings"])
    emit(
        events.TENANT_SETTINGS_CHANGED,
        subject=tenant.public_id,
        tenant=tenant,
        actor={"type": "admin", "id": changed_by.public_id},
        data={"changes": patch, "changed_by": changed_by.public_id},
    )
    return tenant


@transaction.atomic
def mark_domain_verified(
    *, tenant: Tenant, method: str, verified_by: Operator
) -> Tenant:
    tenant.domain_verified_at = timezone.now()
    tenant.save(update_fields=["domain_verified_at"])
    emit(
        events.TENANT_DOMAIN_VERIFIED,
        subject=tenant.public_id,
        tenant=tenant,
        actor={"type": "admin", "id": verified_by.public_id},
        data={"domain": tenant.primary_domain, "method": method},
    )
    return tenant


@transaction.atomic
def complete_setup() -> InstallSetup:
    row = InstallSetup.load()
    row.completed_at = timezone.now()
    row.save(update_fields=["completed_at"])
    return row
