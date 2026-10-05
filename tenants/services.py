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
    settings_command,
)
from audit.outbox import emit
from tenants import operator_invites
from tenants.claim_verification import CLAIM_METHODS, CLAIM_VERIFICATION_BOUNDS
from tenants.mail_settings import (
    HOST_CHANGE_MESSAGE,
    SMTP_SECURITY_MODES,
    host_change_needs_password,
)
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant
from tenants.secrets import delete_secret, has_secret, set_secret


class InvalidTenantSettings(ValueError):
    """``update_tenant_settings`` was given a value core rejects outright
    (spec §9.5: "a value outside the bounds is rejected at configuration
    time, not silently clamped")."""


def _validate_claim_verification(value) -> None:
    if not isinstance(value, dict):
        raise InvalidTenantSettings("claim_verification must be an object")
    methods = value.get("enabled_methods")
    if methods is not None:
        if not isinstance(methods, list) or not all(
            isinstance(m, str) for m in methods
        ):
            raise InvalidTenantSettings(
                "claim_verification.enabled_methods must be a list of strings"
            )
        unknown = [m for m in methods if m not in CLAIM_METHODS]
        if unknown:
            raise InvalidTenantSettings(
                f"claim_verification.enabled_methods has unknown method(s): {unknown!r}"
            )
    ttl = value.get("ttl") or {}
    if not isinstance(ttl, dict):
        raise InvalidTenantSettings("claim_verification.ttl must be an object")
    minutes = ttl.get("domain_email_minutes")
    if minutes is not None:
        bounds = CLAIM_VERIFICATION_BOUNDS["domain_email"]
        if not isinstance(minutes, int) or isinstance(minutes, bool):
            raise InvalidTenantSettings(
                "claim_verification.ttl.domain_email_minutes must be an integer"
            )
        if not (bounds["min_minutes"] <= minutes <= bounds["max_minutes"]):
            raise InvalidTenantSettings(
                "claim_verification.ttl.domain_email_minutes must be between "
                f"{bounds['min_minutes']} and {bounds['max_minutes']}"
            )


def _validate_smtp(value) -> None:
    """``{}`` is valid: the wizard's Skip stores an empty block, which the
    sender reads as unconfigured (decisions.md §4.5)."""
    if not isinstance(value, dict):
        raise InvalidTenantSettings("smtp must be an object")
    if not value:
        return
    security = value.get("security")
    if security not in SMTP_SECURITY_MODES:
        raise InvalidTenantSettings(
            f"smtp.security must be one of {', '.join(SMTP_SECURITY_MODES)}"
        )
    for key in ("host", "from_email", "username"):
        if key in value and not isinstance(value[key], str):
            raise InvalidTenantSettings(f"smtp.{key} must be a string")
    port = value.get("port")
    if port is not None and (
        not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
    ):
        raise InvalidTenantSettings("smtp.port must be an integer from 1 to 65535")
    if (value.get("username") or "").strip() and security == "none":
        raise InvalidTenantSettings(
            "smtp.username requires smtp.security starttls or tls: "
            "credentials are never sent in the clear"
        )


def _validate_adapters(value) -> None:
    """``settings["adapters"]``: ``{adapter_id: {key: scalar}}``. Secrets are
    never in here -- they live in the secret store -- so a value is a string,
    number, boolean or null, and anything nested is refused."""
    if not isinstance(value, dict):
        raise InvalidTenantSettings("adapters must be an object")
    for adapter_id, config in value.items():
        if not isinstance(adapter_id, str) or not adapter_id:
            raise InvalidTenantSettings("an adapter id must be a non-empty string")
        if not isinstance(config, dict):
            raise InvalidTenantSettings(f"adapters.{adapter_id} must be an object")
        for key, item in config.items():
            if not isinstance(key, str) or not (item is None or isinstance(item, (str, int, float, bool))):
                raise InvalidTenantSettings(
                    f"adapters.{adapter_id}.{key} must be a string, number, boolean or null"
                )


def _validate_leads(value) -> None:
    """``{"enabled": bool}``. Lead forms are off until an admin turns them on
    (decisions.md §4.10)."""
    if not isinstance(value, dict):
        raise InvalidTenantSettings("leads must be an object")
    unknown = set(value) - {"enabled"}
    if unknown:
        raise InvalidTenantSettings(f"leads has unknown key(s): {sorted(unknown)!r}")
    if "enabled" in value and not isinstance(value["enabled"], bool):
        raise InvalidTenantSettings("leads.enabled must be true or false")


# One validator per settings key that core enforces bounds on (spec §9.5).
# A key with no validator is merged unchecked, as before.
_SETTINGS_VALIDATORS = {
    "claim_verification": _validate_claim_verification,
    "smtp": _validate_smtp,
    "adapters": _validate_adapters,
    "leads": _validate_leads,
}


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

    No password is set (``set_unusable_password``) and no invite is minted: the
    operator has no usable credential until ``manage.py issue_operator_invite``
    prints them a set-password link (decisions.md §4.14). ``is_superadmin`` is not a
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

    An operator minted by this call also gets a set-password invite (decisions.md
    §4.14), mailed through ``tenant``'s SMTP; the membership activates when they
    spend it. When ``tenant`` cannot send that mail no invite is minted and the
    row says so (``problem.invite_mail``). The payload names the role and, once
    applied, the operator's id -- never the email (#219).

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
        payload={"role": int(role)},
    )
    try:
        membership, event_id, invite_mail = _apply_invite_staff(
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
    log_conclude(
        row,
        outcome="applied",
        result_event_id=event_id,
        problem={"invite_mail": invite_mail} if invite_mail else None,
        payload={"operator_id": membership.operator.public_id, "role": int(role)},
    )
    return membership


@transaction.atomic
def _apply_invite_staff(
    *, tenant: Tenant, email: str, role: int, invited_by: Operator, actor: dict
) -> "tuple[StaffMembership, str, str]":
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
    invite_mail = ""
    if created:
        if operator_invites.mail_available(tenant):
            operator_invites.mint_invite(
                operator=operator,
                invited_by=invited_by,
                membership=membership,
                mail_tenant=tenant,
                throttle=True,
            )
        else:
            invite_mail = "unavailable"
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
    return membership, event.event_id, invite_mail


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


def set_tenant_domain(*, tenant: Tenant, domain: str, changed_by: Operator) -> Tenant:
    """The wizard's domain step. An operator command: one ``settings.update`` row
    (spec §11.2, decisions.md §4), so it runs in autocommit."""
    domain = domain.strip().rstrip(".").lower()
    with settings_command(tenant, changed_by, page="domain") as cmd:
        if domain != (tenant.primary_domain or ""):
            cmd.named("domain")
        _apply_tenant_domain(tenant=tenant, domain=domain, changed_by=changed_by)
    return tenant


@transaction.atomic
def _apply_tenant_domain(*, tenant: Tenant, domain: str, changed_by: Operator) -> Tenant:
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
    for key, value in changes.items():
        validator = _SETTINGS_VALIDATORS.get(key)
        if validator:
            validator(value)
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


def update_mail_settings(
    *,
    tenant: Tenant,
    config: dict,
    password: str = "",
    clear_password: bool = False,
    changed_by: Operator,
) -> Tenant:
    """Write the ``smtp`` block and its password secret together
    (decisions.md §4.5), so a rejected block never leaves a new password
    behind. A blank ``password`` keeps the stored secret; ``clear_password``
    removes it; clearing the username removes it too, since nothing would
    use it.

    The stored secret is never sent to a new host: a host change with a
    username set and no password supplied is refused. Checked here, not in
    ``_validate_smtp``, because it needs the stored block and whether a
    password arrived -- neither of which a value-only validator sees.

    An operator command: one ``settings.update`` row naming the fields that
    changed (decisions.md §4), so it runs in autocommit.
    """
    before = dict((tenant.settings or {}).get("smtp") or {})
    had_password = has_secret("smtp_password", tenant=tenant)
    with settings_command(tenant, changed_by, page="mail", refused=(InvalidTenantSettings,)) as cmd:
        _apply_mail_settings(
            tenant=tenant, config=config, password=password,
            clear_password=clear_password, changed_by=changed_by,
        )
        cmd.changed(before, (tenant.settings or {}).get("smtp"))
        if password or (had_password and not has_secret("smtp_password", tenant=tenant)):
            cmd.named("password")
    return tenant


@transaction.atomic
def _apply_mail_settings(
    *, tenant: Tenant, config: dict, password: str, clear_password: bool, changed_by: Operator
) -> None:
    username = (config.get("username") or "").strip()
    if (
        not password
        and not clear_password
        and host_change_needs_password(
            tenant, host=config.get("host") or "", username=username
        )
    ):
        raise InvalidTenantSettings(HOST_CHANGE_MESSAGE)
    update_tenant_settings(
        tenant=tenant, changes={"smtp": config}, changed_by=changed_by
    )
    if password:
        set_secret("smtp_password", password, tenant=tenant)
    elif clear_password or not username:
        delete_secret("smtp_password", tenant=tenant)


def skip_mail_setup(*, tenant: Tenant, changed_by: Operator) -> Tenant:
    """The wizard's Skip: an empty ``smtp`` block. The wizard counts the step
    done once the key exists, and the sender reads an empty host as
    unconfigured (decisions.md §4.5). Logged like any other mail save."""
    before = dict((tenant.settings or {}).get("smtp") or {})
    had_password = has_secret("smtp_password", tenant=tenant)
    with settings_command(tenant, changed_by, page="mail") as cmd:
        _apply_skip_mail_setup(tenant=tenant, changed_by=changed_by)
        cmd.changed(before, {})
        if had_password:
            cmd.named("password")
    return tenant


@transaction.atomic
def _apply_skip_mail_setup(*, tenant: Tenant, changed_by: Operator) -> None:
    update_tenant_settings(tenant=tenant, changes={"smtp": {}}, changed_by=changed_by)
    delete_secret("smtp_password", tenant=tenant)


def save_settings_page(
    tenant: Tenant, *, page: str, block: str, value: dict, changed_by: Operator,
    secrets: "dict[str, str] | None" = None,
) -> Tenant:
    """Save one settings block from an operator page: the lead form, and the
    wizard's storage and claims steps. ``secrets`` maps a form field name to a
    value stored as ``"<block>_<name>"``; the command-log row names the field and
    never the value. An operator command, so it runs in autocommit
    (decisions.md §4)."""
    before = dict((tenant.settings or {}).get(block) or {})
    with settings_command(tenant, changed_by, page=page, refused=(InvalidTenantSettings,)) as cmd:
        _apply_settings_page(
            tenant=tenant, block=block, value=value, changed_by=changed_by, secrets=secrets or {},
        )
        cmd.changed(before, (tenant.settings or {}).get(block))
        cmd.named(*(secrets or {}))
    return tenant


@transaction.atomic
def _apply_settings_page(
    *, tenant: Tenant, block: str, value: dict, changed_by: Operator, secrets: dict
) -> None:
    update_tenant_settings(tenant=tenant, changes={block: value}, changed_by=changed_by)
    for name, secret in secrets.items():
        set_secret(f"{block}_{name}", secret, tenant=tenant)


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
