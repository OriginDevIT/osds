"""Service layer for principals and tenancy.

Views, management commands and (later) adapters call these; nothing else writes
these models.

Two shapes live here. ``create_tenant``, ``create_operator`` and
``invite_staff`` are command orchestrators (spec §11.2, the shape of
``directory.services.upsert_listing``): they run in autocommit, write the
command log before and after an atomic ``_apply_*`` that makes the state
change and emits its event, and refuse to run inside a caller's transaction.
Everything else -- ``create_superadmin``, ``add_bootstrap_membership``,
``set_tenant_domain``, ``update_tenant_settings``, ``mark_domain_verified``,
``complete_setup`` -- runs in a single transaction and writes any outbox row
in it.
"""

from __future__ import annotations

import secrets as pysecrets

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit import events
from audit.command_log import (
    MustNotBeInTransaction,
    log_conclude,
    log_received,
    require_autocommit,
)
from audit.outbox import emit
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant


def _role_key(role: int) -> str:
    return StaffMembership.Role(role).name.lower()


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


@transaction.atomic
def create_superadmin(*, email: str, password: str, name: str = "") -> Operator:
    """The first-run superadmin. No event -- operator creation emits nothing,
    and superadmin status is command-log-only (spec §4.4)."""
    return Operator.objects.create_superuser(
        email=email, password=password, name=name
    )


def create_tenant(*, name: str, slug: str, mode: str, created_by: Operator) -> Tenant:
    """Create a directory and emit ``tenant.created``.

    Orchestrator shape: the command log is written outside the state-change
    transaction so it survives a rollback of the command it records (spec
    §11.2). The received row carries a null tenant -- the tenant does not exist
    yet -- and ``log_conclude`` does not backfill it (#164).
    """
    require_autocommit()
    actor = {"type": "admin", "id": created_by.public_id}
    row = log_received(
        command="tenant.create",
        tenant=None,
        idempotency_key=None,
        actor=actor,
        trace_id=None,
        origin="",
        payload={"name": name, "slug": slug, "mode": mode},
    )
    tenant, event_id = _apply_create_tenant(
        name=name, slug=slug, mode=mode, created_by=created_by, actor=actor
    )
    log_conclude(row, outcome="applied", result_event_id=event_id)
    return tenant


@transaction.atomic
def _apply_create_tenant(
    *, name: str, slug: str, mode: str, created_by: Operator, actor: dict
) -> "tuple[Tenant, str]":
    tenant = Tenant.objects.create(
        name=name, slug=slug, mode=mode, created_by=created_by
    )
    event = emit(
        events.TENANT_CREATED,
        subject=tenant.public_id,
        tenant=tenant,
        actor=actor,
        data={
            "slug": tenant.slug,
            "mode": tenant.mode,
            "created_by": created_by.public_id,
        },
    )
    return tenant, event.event_id


def create_operator(
    *, email: str, name: str = "", created_by: Operator
) -> Operator:
    """Create an operator identity. Emits nothing -- operator creation is
    command-log-only, with a null tenant (spec §4.4, #146); the command log is
    the whole audit trail for it.

    No password is set (``set_unusable_password``); the operator has no usable
    credential until a set-password flow exists. ``is_superadmin`` is not a
    parameter -- elevation is installation-scoped authorization and is out of
    scope here (#165).
    """
    require_autocommit()
    email = _norm_email(email)
    row = log_received(
        command="operator.create",
        tenant=None,
        idempotency_key=None,
        actor={"type": "admin", "id": created_by.public_id},
        trace_id=None,
        origin="",
        payload={"email": email, "name": name},
    )
    operator = _apply_create_operator(email=email, name=name)
    log_conclude(row, outcome="applied")
    return operator


@transaction.atomic
def _apply_create_operator(*, email: str, name: str) -> Operator:
    operator = Operator(email=email, name=name)
    operator.set_unusable_password()
    operator.save()
    return operator


def invite_staff(
    *, tenant: Tenant, email: str, role: int, invited_by: Operator
) -> StaffMembership:
    """Invite an operator to ``tenant``. Writes a pending membership and emits
    ``staff.invited`` -- never ``staff.accepted`` on this path, and never a
    write to an existing operator row (spec §4.4). The outcome is the same
    whether or not the email already had an account.

    A duplicate ``(operator, tenant)`` membership is concluded ``rejected``.
    The ``IntegrityError`` is caught here, *outside* ``_apply_invite_staff`` --
    caught inside its own ``atomic`` block it would leave the connection
    unusable for the rest of the request.
    """
    require_autocommit()
    email = _norm_email(email)
    actor = {"type": "admin", "id": invited_by.public_id}
    row = log_received(
        command="staff.invite",
        tenant=tenant,
        idempotency_key=None,
        actor=actor,
        trace_id=None,
        origin="",
        payload={"email": email, "role": int(role)},
    )
    try:
        membership, event_id = _apply_invite_staff(
            tenant=tenant,
            email=email,
            role=role,
            invited_by=invited_by,
            actor=actor,
        )
    except IntegrityError:
        log_conclude(
            row, outcome="rejected", problem={"error": "duplicate_membership"}
        )
        raise
    log_conclude(row, outcome="applied", result_event_id=event_id)
    return membership


@transaction.atomic
def _apply_invite_staff(
    *, tenant: Tenant, email: str, role: int, invited_by: Operator, actor: dict
) -> "tuple[StaffMembership, str]":
    operator, created = Operator.objects.get_or_create(email=email)
    if created:
        operator.set_unusable_password()
        operator.save(update_fields=["password"])
    membership = StaffMembership.objects.create(
        operator=operator,
        tenant=tenant,
        role=role,
        status=StaffMembership.Status.PENDING,
        invited_by=invited_by,
    )
    member = {
        "operator_id": operator.public_id,
        "role": _role_key(role),
        "status": "pending",
    }
    event = emit(
        events.STAFF_INVITED,
        subject=operator.public_id,
        tenant=tenant,
        actor=actor,
        data={
            "membership": member,
            "operator": {"email": operator.email, "existing": not created},
            "granted_by": invited_by.public_id,
        },
    )
    return membership, event.event_id


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
