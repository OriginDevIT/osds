"""Operator invitation: the set-password link (decisions.md §4.14, spec §4.4).

An operator minted by an invitation has no credential. The invite mails a link
on the console host; a POST from the page it opens sets the password, spends
the token and activates the membership minted with the operator -- accepting
the invitation and the membership are one act.

* **The token** is 256 random bits stored only as a SHA-256 digest, valid for
  seven days, used once. Following the link spends nothing: a mail scanner's
  GET must not burn it.
* **Mail** goes through the inviting tenant's SMTP (``OutboundMessage.tenant``
  is non-null), and only when that tenant can send mail and the console has a
  host to link to. Otherwise no invite is minted; ``issue_operator_invite``
  prints one.
* **An existing operator is never touched** (spec §4.4). Nothing here mints a
  token for one, and the consume path refuses an operator who has gained a
  usable password since the invite was issued.

Command-log rows carry no email, no password and no token.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit import events
from audit.command_log import log_conclude, log_received, require_autocommit
from audit.mail import enqueue
from audit.models import OutboundMessage
from audit.outbox import emit
from osds.adapters import email_available
from tenants.models import Operator, OperatorInvite, StaffMembership, Tenant

INVITE_TTL = timedelta(days=7)
MAIL_KIND = "operator.invite"

# Per address, from OperatorInvite.created_at (the same shape as §4.9's
# per-account limit). Applies to the admin path; the command line has none.
MAX_ISSUES_PER_HOUR = 3
MIN_SECONDS_BETWEEN_ISSUES = 60


class InviteRefused(Exception):
    """The link could not be spent: unknown, expired, used, no longer for an
    operator who needs it. One page for the visitor; the reason is for the log."""

    def __init__(self, reason: str = "invalid"):
        self.reason = reason
        super().__init__(reason)


class PasswordRejected(Exception):
    """The password failed validation. Nothing was spent."""

    def __init__(self, messages: "list[str]"):
        self.messages = messages
        super().__init__("password_rejected")


class IssueThrottled(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class IssuedInvite:
    invite: OperatorInvite
    secret: str  # plaintext; exists in the mail or on the operator's terminal only


def _digest(secret: str) -> str:
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def console_host() -> str:
    return (getattr(settings, "OSDS_CONSOLE_HOST", "") or "").strip().rstrip(".").lower()


def invite_url(secret: str) -> "str | None":
    """Always https, whatever ``OSDS_SECURE_COOKIES`` says (§4.9's precedent): an
    HTTP-only console uses ``issue_operator_invite`` and a path it types."""
    host = console_host()
    return f"https://{host}/invite/{secret}/" if host else None


def mail_available(tenant: Tenant) -> bool:
    """Whether an invitation from ``tenant`` can carry its link: mail that can
    be sent, and a console host to point at. A property of the tenant and the
    install, never of the address, so reporting it reveals nothing about who
    already has an account."""
    return bool(console_host()) and email_available(tenant)


def _throttled(operator: Operator, *, now) -> "str | None":
    recent = OperatorInvite.objects.filter(operator=operator).order_by("-created_at")
    last = recent.first()
    if last is not None and (now - last.created_at).total_seconds() < MIN_SECONDS_BETWEEN_ISSUES:
        return "too_soon"
    if recent.filter(created_at__gt=now - timedelta(hours=1)).count() >= MAX_ISSUES_PER_HOUR:
        return "hourly_cap"
    return None


def mint_invite(
    *,
    operator: Operator,
    invited_by: "Operator | None",
    membership: "StaffMembership | None",
    mail_tenant: "Tenant | None" = None,
    throttle: bool = False,
    now=None,
) -> IssuedInvite:
    """Mint an invite inside the caller's transaction. A newer link supersedes
    every earlier unused one and kills its mail if it has not gone out.

    With ``mail_tenant`` the link is queued as mail to the operator, atomically
    with the invite; without it nothing is mailed and the caller shows the
    plaintext. ``throttle`` applies the per-address limit and raises
    ``IssueThrottled`` before any write."""
    now = now or timezone.now()
    if throttle:
        why = _throttled(operator, now=now)
        if why is not None:
            raise IssueThrottled(why)

    for old in OperatorInvite.objects.select_for_update().filter(
        operator=operator, used_at__isnull=True, expires_at__gt=now
    ):
        old.expires_at = now
        old.save(update_fields=["expires_at"])
        pending = old.message
        if pending is not None and pending.status == OutboundMessage.Status.PENDING:
            pending.expires_at = now
            pending.save(update_fields=["expires_at"])

    secret = secrets.token_urlsafe(32)
    expires_at = now + INVITE_TTL
    message = None
    if mail_tenant is not None:
        # The invite names no inviter, tenant or role: an inviter can type
        # anything, and this goes out from the operator's own domain.
        message = enqueue(
            tenant=mail_tenant,
            kind=MAIL_KIND,
            to_address=operator.email,
            subject="You have been invited to administer a directory",
            body_text=(
                "Use this link to set a password and accept the invitation:\n\n"
                f"{invite_url(secret)}\n\n"
                f"It works once and expires at {expires_at.strftime('%Y-%m-%d %H:%M')} UTC. "
                "If you did not expect it, ignore this email: nobody can set a "
                "password without the link."
            ),
            expires_at=expires_at,
        )
    invite = OperatorInvite.objects.create(
        operator=operator,
        membership=membership,
        message=message,
        token_hash=_digest(secret),
        expires_at=expires_at,
        invited_by=invited_by,
        created_at=now,
    )
    return IssuedInvite(invite=invite, secret=secret)


# --- the command-line issue path ------------------------------------------------

ISSUE_ACTOR = {"type": "system", "id": "issue_operator_invite"}


class IssueRefused(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def issue_invite(*, email: str, now=None) -> IssuedInvite:
    """The ``operator.issue_invite`` command, run from the host shell: mint a
    link for an operator who has no usable password and return it for printing.
    No mail, and no throttle -- shell access is already full control. Raises
    ``IssueRefused`` for an unknown operator, an inactive one, or one who
    already has a password (their credential is not ours to replace)."""
    require_autocommit()
    now = now or timezone.now()
    row = log_received(
        command="operator.issue_invite", tenant=None, idempotency_key=None,
        actor=ISSUE_ACTOR, trace_id=None, origin="", payload={},
    )
    try:
        issued = _apply_issue(email=(email or "").strip().lower(), now=now)
    except IssueRefused as exc:
        log_conclude(row, outcome="rejected", problem={"reason": exc.reason})
        raise
    log_conclude(
        row, outcome="applied",
        payload={
            "operator_id": issued.invite.operator.public_id,
            "membership": issued.invite.membership_id is not None,
        },
    )
    return issued


@transaction.atomic
def _apply_issue(*, email: str, now) -> IssuedInvite:
    operator = Operator.objects.select_for_update().filter(email=email).first()
    if operator is None:
        raise IssueRefused("unknown_operator")
    if not operator.is_active:
        raise IssueRefused("inactive")
    if operator.has_usable_password():
        raise IssueRefused("has_password")
    # Re-attach the membership minted with this operator, if it is still
    # pending: an expired invite must not strand it.
    prior = (
        OperatorInvite.objects.filter(operator=operator, membership__isnull=False)
        .select_related("membership")
        .order_by("-created_at")
        .first()
    )
    membership = (
        prior.membership
        if prior is not None and prior.membership.status == StaffMembership.Status.PENDING
        else None
    )
    return mint_invite(
        operator=operator, invited_by=None, membership=membership, now=now
    )


# --- spending the link -----------------------------------------------------------


def peek_invite(secret: str, *, now=None) -> "OperatorInvite | None":
    """The invite if it could still be spent, with no state change: this is what
    the page's GET calls."""
    now = now or timezone.now()
    invite = (
        OperatorInvite.objects.select_related("operator")
        .filter(token_hash=_digest(secret), used_at__isnull=True, expires_at__gt=now)
        .first()
    )
    if invite is None:
        return None
    op = invite.operator
    if not op.is_active or op.has_usable_password():
        return None
    return invite


def set_password(*, secret: str, password: str, now=None) -> Operator:
    """The ``operator.set_password`` command: spend the link, set the password,
    activate the membership minted with the operator. The actor is a visitor
    with no id and the payload is empty -- no email, no password, no token. A
    rejected password concludes ``rejected`` and spends nothing."""
    require_autocommit()
    now = now or timezone.now()
    row = log_received(
        command="operator.set_password", tenant=None, idempotency_key=None,
        actor={"type": "visitor", "id": ""}, trace_id=None, origin="", payload={},
    )
    try:
        operator, event_id = _apply_set_password(secret=secret, password=password, now=now)
    except InviteRefused as exc:
        log_conclude(row, outcome="rejected", problem={"reason": exc.reason})
        raise
    except PasswordRejected:
        log_conclude(row, outcome="rejected", problem={"reason": "password_rejected"})
        raise
    log_conclude(row, outcome="applied", result_event_id=event_id)
    return operator


@transaction.atomic
def _apply_set_password(*, secret: str, password: str, now) -> "tuple[Operator, str]":
    invite = (
        OperatorInvite.objects.select_for_update(of=("self",))
        .select_related("membership__tenant")
        .filter(token_hash=_digest(secret))
        .first()
    )
    if invite is None:
        raise InviteRefused("unknown")
    if invite.used_at is not None:
        raise InviteRefused("used")
    if invite.expires_at <= now:
        raise InviteRefused("expired")
    operator = Operator.objects.select_for_update().get(pk=invite.operator_id)
    if not operator.is_active:
        raise InviteRefused("inactive")
    if operator.has_usable_password():
        raise InviteRefused("has_password")
    try:
        validate_password(password, operator)
    except ValidationError as exc:
        raise PasswordRejected(list(exc.messages)) from None

    operator.set_password(password)
    operator.save(update_fields=["password"])
    invite.used_at = now
    invite.save(update_fields=["used_at"])

    event_id = ""
    membership = invite.membership
    if membership is not None and membership.status == StaffMembership.Status.PENDING:
        membership.status = StaffMembership.Status.ACTIVE
        membership.accepted_at = now
        membership.save(update_fields=["status", "accepted_at"])
        role = StaffMembership.Role(membership.role)
        event = emit(
            events.STAFF_ACCEPTED,
            subject=operator.public_id,
            tenant=membership.tenant,
            actor={
                "type": "admin" if role == StaffMembership.Role.ADMIN else "staff",
                "id": operator.public_id,
            },
            data={
                "membership": {
                    "operator_id": operator.public_id,
                    "role": role.name.lower(),
                    "status": "active",
                }
            },
        )
        event_id = event.event_id
    return operator, event_id
