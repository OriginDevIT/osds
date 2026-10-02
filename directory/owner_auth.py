"""Owner sign-in: the emailed link, the session it opens and the guard that
reads it (spec §4.3, decisions.md §4.9).

An owner is a ``DirectoryUser`` for whom ``Listing.owner`` currently points at
them. There is no password and no credential on the user row: a link re-proves
control of the mailbox on every sign-in (§4.3).

* **The link** is 256 random bits, stored only as a SHA-256 digest, valid for
  12 hours, and used once. It is consumed by a POST from a confirm page, never
  by following it, so a mail scanner's GET cannot burn it.
* **The session** has its own table and cookie, apart from the operator's
  Django session. It ends after 12 hours without activity and after 30 days
  regardless.
* **Authorization is live.** Nothing about ownership is stored in the session;
  every owner route resolves the listing through ``Listing.owner``, so an owner
  who loses a listing loses it at once.

Commands follow the orchestrator shape (``require_autocommit``,
``log_received``, an atomic ``_apply_*``, ``log_conclude``) with the actor a
visitor and nothing identifying in the payload: the email never reaches the
command log. No event is emitted; spec §3.3 has no sign-in event.

Call with the tenant in ambient scope.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect
from django.utils import timezone

from audit.command_log import log_conclude, log_received, require_autocommit
from audit.mail import enqueue
from audit.models import OutboundMessage
from audit.ratelimit import RateLimited
from directory import owner_limits, routing
from directory.models import DirectoryUser, Listing, OwnerSession, OwnerSignInToken
from osds.adapters import email_available

logger = logging.getLogger("osds.owner_auth")

SIGNIN_TTL = timedelta(hours=12)  # spec §4.3: a core-computed 12-hour link
SESSION_IDLE = timedelta(hours=12)  # spec §4.3: 12 hours without activity
SESSION_ABSOLUTE = timedelta(days=30)  # decisions.md §4.9: a hard cap as well
TOUCH_INTERVAL = timedelta(seconds=60)  # last_seen_at is written at most this often

# Per account (decisions.md §4.9): derived from the token rows, so it is
# exactly "mail sent to this owner", with no table of its own.
MAX_LINKS_PER_HOUR = 3
MIN_SECONDS_BETWEEN_LINKS = 60

COOKIE_NAME = "osds_owner"
COOKIE_PATH = "/owner/"
MAIL_KIND = "owner.signin_link"


class SignInRefused(Exception):
    """The link could not be exchanged: unknown, expired, used or no longer
    attached to an owner. One reason to the visitor; the code is for the log."""

    def __init__(self, reason: str = "invalid"):
        self.reason = reason
        super().__init__(reason)


def _digest(secret: str) -> str:
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def sign_in_available(tenant) -> bool:
    """A link needs mail that can be sent and an address a mail client can
    open. Without a verified domain the link is a relative path, which is the
    whole message, so sign-in is unavailable rather than half-working."""
    return email_available(tenant) and routing.has_absolute_base(tenant)


def owns_a_listing(user: DirectoryUser) -> bool:
    return Listing.objects.filter(owner=user).exists()


# --- request a link -----------------------------------------------------------


def _expire_pending_message(message: "OutboundMessage | None", *, now) -> None:
    if message is not None and message.status == OutboundMessage.Status.PENDING:
        message.expires_at = now
        message.save(update_fields=["expires_at"])


def _throttled(user: DirectoryUser, *, now) -> "str | None":
    """Why this owner may not be sent another link right now, or ``None``."""
    recent = OwnerSignInToken.objects.filter(user=user).order_by("-created_at")
    last = recent.first()
    if last is not None and (now - last.created_at).total_seconds() < MIN_SECONDS_BETWEEN_LINKS:
        return "too_soon"
    if recent.filter(created_at__gt=now - timedelta(hours=1)).count() >= MAX_LINKS_PER_HOUR:
        return "hourly_cap"
    return None


def request_sign_in(tenant, *, email: str, ip: "str | None", now=None) -> str:
    """The ``owner.request_signin`` command. Returns ``"sent"``, ``"ignored"``
    (no such owner, or throttled) or ``"unavailable"``; the view answers
    ``sent`` and ``ignored`` identically, since a different response would
    tell a stranger which addresses own a listing. Raises ``RateLimited`` for
    the per-IP limit, which depends on the caller and not on the address."""
    require_autocommit()
    now = now or timezone.now()
    address = (email or "").strip().lower()
    actor = {"type": "visitor", "id": ""}

    # Per IP (#210), ahead of the received row so a flood writes one blocked
    # row per window and not one per request.
    verdict = owner_limits.check_request(tenant, ip=ip, now=now)
    if not verdict.allowed:
        if verdict.first_block:
            blocked = log_received(
                command="owner.request_signin", tenant=tenant, idempotency_key=None,
                actor=actor, trace_id=None, origin="", payload={},
            )
            log_conclude(blocked, outcome="blocked", problem={"rate_limited": verdict.rule})
        raise RateLimited(verdict)

    row = log_received(
        command="owner.request_signin", tenant=tenant, idempotency_key=None,
        actor=actor, trace_id=None, origin="", payload={},
    )
    outcome, reason = _apply_request(tenant, address=address, now=now)
    if outcome == "sent":
        log_conclude(row, outcome="applied")
    else:
        # Never the address: the log is permanent and the reason is enough.
        log_conclude(row, outcome="rejected", problem={"reason": reason})
    return outcome


@transaction.atomic
def _apply_request(tenant, *, address: str, now) -> "tuple[str, str]":
    if not sign_in_available(tenant):
        return "unavailable", "unavailable"
    user = DirectoryUser.objects.filter(email=address).first() if address else None
    if user is None or not owns_a_listing(user):
        return "ignored", "not_an_owner"
    why = _throttled(user, now=now)
    if why is not None:
        return "ignored", why

    # A new link supersedes every earlier unused one, and kills their mail if
    # it has not gone out: only the latest link works.
    for old in OwnerSignInToken.objects.select_for_update().filter(
        user=user, used_at__isnull=True, expires_at__gt=now
    ):
        old.expires_at = now
        old.save(update_fields=["expires_at"])
        _expire_pending_message(old.message, now=now)

    secret = secrets.token_urlsafe(32)
    expires_at = now + SIGNIN_TTL
    url = routing.absolute_url(tenant, f"/owner/signin/{secret}/")
    # No name, listing or claimant text: the user row's name was supplied by
    # whoever first claimed with this address.
    message = enqueue(
        tenant=tenant,
        kind=MAIL_KIND,
        to_address=user.email,
        subject=f"Your sign-in link for {tenant.name}",
        body_text=(
            f"Use this link to sign in and manage your listing on {tenant.name}:\n\n"
            f"{url}\n\n"
            f"It works once and expires at {expires_at.strftime('%Y-%m-%d %H:%M')} UTC. "
            f"If you did not ask for it, ignore this email: nobody can sign in "
            f"without the link."
        ),
        expires_at=expires_at,
    )
    OwnerSignInToken.objects.create(
        tenant=tenant, user=user, token_hash=_digest(secret),
        expires_at=expires_at, message=message,
    )
    return "sent", ""


# --- exchange the link for a session ------------------------------------------


def peek_token(tenant, secret: str, *, now=None) -> "OwnerSignInToken | None":
    """The token if it could still be exchanged, with no state change: this is
    what the confirm page's GET calls."""
    now = now or timezone.now()
    token = (
        OwnerSignInToken.objects.select_related("user")
        .filter(token_hash=_digest(secret), used_at__isnull=True, expires_at__gt=now)
        .first()
    )
    if token is None or not owns_a_listing(token.user):
        return None
    return token


@dataclass
class NewSession:
    secret: str  # the cookie value
    session: OwnerSession


def sign_in(tenant, *, secret: str, now=None) -> NewSession:
    """The ``owner.sign_in`` command: spend the link, open a session. Raises
    ``SignInRefused`` for anything that is not a live link."""
    require_autocommit()
    now = now or timezone.now()
    row = log_received(
        command="owner.sign_in", tenant=tenant, idempotency_key=None,
        actor={"type": "visitor", "id": ""}, trace_id=None, origin="", payload={},
    )
    try:
        new = _apply_sign_in(tenant, secret=secret, now=now)
    except SignInRefused as exc:
        log_conclude(row, outcome="rejected", problem={"reason": exc.reason})
        raise
    log_conclude(row, outcome="applied")
    return new


@transaction.atomic
def _apply_sign_in(tenant, *, secret: str, now) -> NewSession:
    token = (
        OwnerSignInToken.objects.select_for_update()
        .select_related("user")
        .filter(token_hash=_digest(secret))
        .first()
    )
    if token is None:
        raise SignInRefused("unknown")
    if token.used_at is not None:
        raise SignInRefused("used")
    if token.expires_at <= now:
        raise SignInRefused("expired")
    if not owns_a_listing(token.user):
        raise SignInRefused("not_an_owner")
    token.used_at = now
    token.save(update_fields=["used_at"])
    cookie = secrets.token_urlsafe(32)
    session = OwnerSession.objects.create(
        tenant=tenant, user=token.user, token_hash=_digest(cookie),
        last_seen_at=now, expires_at=now + SESSION_ABSOLUTE,
    )
    return NewSession(cookie, session)


# --- the session --------------------------------------------------------------


def resolve_session(tenant, cookie: str, *, now=None) -> "OwnerSession | None":
    """The live session for ``cookie``, or ``None``. An expired one is deleted
    on sight. ``last_seen_at`` moves at most once a minute, so reading a page
    is not a write per request."""
    if not cookie:
        return None
    now = now or timezone.now()
    session = (
        OwnerSession.objects.select_related("user")
        .filter(token_hash=_digest(cookie))
        .first()
    )
    if session is None:
        return None
    if session.expires_at <= now or session.last_seen_at + SESSION_IDLE <= now:
        session.delete()
        return None
    if now - session.last_seen_at >= TOUCH_INTERVAL:
        OwnerSession.objects.filter(pk=session.pk).update(last_seen_at=now)
        session.last_seen_at = now
    return session


def sign_out(tenant, *, session: OwnerSession, everywhere: bool = False) -> int:
    """The ``owner.sign_out`` command: end this session, or every session this
    owner holds on this directory. Returns how many were ended."""
    require_autocommit()
    row = log_received(
        command="owner.sign_out", tenant=tenant, idempotency_key=None,
        actor={"type": "owner", "id": session.user.public_id}, trace_id=None,
        origin="", payload={"everywhere": everywhere},
    )
    qs = OwnerSession.objects.filter(user=session.user) if everywhere else (
        OwnerSession.objects.filter(pk=session.pk)
    )
    ended, _ = qs.delete()
    log_conclude(row, outcome="applied")
    return ended


# --- the guard ----------------------------------------------------------------


def cookie_kwargs() -> dict:
    """The owner cookie's attributes: host-only, HttpOnly, Lax (the sign-in
    arrives by a top-level navigation from a mail client), Secure by the same
    switch as the operator cookies, and scoped to ``/owner/``."""
    return {
        "max_age": int(SESSION_ABSOLUTE.total_seconds()),
        "path": COOKIE_PATH,
        "domain": None,
        "secure": settings.SESSION_COOKIE_SECURE,
        "httponly": True,
        "samesite": "Lax",
    }


def owner_required(view):
    """Require a live owner session on a tenant host. Anonymous goes to the
    sign-in page; the operator's session never satisfies this."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        tenant = getattr(request, "tenant", None)
        if getattr(request, "osds_host_kind", None) != "tenant" or tenant is None:
            raise Http404()
        session = resolve_session(tenant, request.COOKIES.get(COOKIE_NAME, ""))
        if session is None:
            response = redirect("owner-signin")
            if request.COOKIES.get(COOKIE_NAME):
                response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
            return response
        request.owner = session.user
        request.owner_session = session
        return view(request, *args, **kwargs)

    return wrapped
