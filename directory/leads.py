"""Lead capture (spec §3.3 ``lead.*``, §7 ``lead.create`` / ``lead.markSpam``,
§9.0; decisions.md §4.10).

``create_lead`` is the ``lead.create`` command. Like ``submit_claim`` it is
public and visitor-originated, refuses to run inside an open transaction, and
writes its command-log rows outside the command's own. Unlike a claim it mints
no ``DirectoryUser``: only ``claim.submit`` does. A lead is a consumer's message
to a business, a ``Consent`` row recording that they agreed to be contacted, and
a ``lead.captured`` event -- and, unless it looks like spam, a notice to whoever
can act on it (``directory.lead_notices``).

``mark_lead_spam`` is the moderator's ``lead.mark_spam``.

``lead.delivered`` and ``lead.delivery_failed`` are not emitted: sending mail
emits no event (decisions.md §4.3), so there is no point at which core could
know. ``Lead.status`` stays ``captured``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from audit import events
from audit.command_log import log_conclude, log_received, require_autocommit
from audit.outbox import emit
from audit.ratelimit import RateLimited
from directory import lead_limits, lead_notices, normalize
from directory.claim_review import _membership, _operator_actor
from directory.field_schema import SchemaError
from directory.models import Consent, ConsentText, Lead, Listing
from directory.services import ConsentRequired
from osds.client_ip import storable_ip
from tenants.models import StaffMembership

LEAD_CONSENT_TEXT_KEY = "lead-consent"
LEAD_CONSENT_CHANNEL = "contact_by_business"

# Neutral, narrow wording: it covers contact about this inquiry and nothing
# else. Counsel review is tracked separately, and no admin UI publishes a new
# version yet, so v1 is what every tenant shows until one does.
DEFAULT_LEAD_CONSENT_BODY = (
    "By sending this message you agree that the business named above may "
    "contact you about your inquiry, using the email address and, if you gave "
    "one, the phone number you provided. We pass your message and contact "
    "details on to the business and keep a record of this agreement."
)

NAME_MAX = 100
MESSAGE_MIN = 10
MESSAGE_MAX = 1000
DUPLICATE_WINDOW = timedelta(hours=1)
SPAM_THRESHOLD = Decimal("0.800")
MODERATE_ROLE = StaffMembership.Role.MODERATOR

_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_RUN = re.compile(r"(.)\1{7,}")


def leads_enabled(tenant) -> bool:
    """The tenant switch (default off). Set from the admin settings page."""
    return bool(((tenant.settings or {}).get("leads") or {}).get("enabled"))


def get_lead_consent_text(tenant) -> ConsentText:
    """The current lead-consent wording for ``tenant`` (spec §9.0), seeded
    lazily on first read exactly as claims' is (``services.get_default_consent_text``)."""
    text, _ = ConsentText.objects.get_or_create(
        tenant=tenant,
        key=LEAD_CONSENT_TEXT_KEY,
        version="v1",
        defaults={"body": DEFAULT_LEAD_CONSENT_BODY},
    )
    return text


def clean_message(value) -> str:
    """Plain text only: control characters dropped, line endings normalised,
    runs of blank lines collapsed. Never HTML-interpreted anywhere."""
    text = _CONTROL.sub("", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@dataclass(frozen=True)
class SpamVerdict:
    score: Decimal
    signals: "tuple[str, ...]"


def spam_score(*, name: str, email: str, message: str) -> SpamVerdict:
    """A small, deterministic heuristic -- not a classifier. Each signal adds a
    fixed weight; a lead at or above ``SPAM_THRESHOLD`` is marked spam and not
    delivered, and a moderator can still read it."""
    score = Decimal("0")
    signals: list[str] = []
    links = len(_URL.findall(message))
    if links:
        score += min(Decimal("0.35") * links, Decimal("0.70"))
        signals.append("links_in_message")
    letters = [c for c in message if c.isalpha()]
    if len(letters) >= 20 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        score += Decimal("0.20")
        signals.append("shouting")
    if _RUN.search(message):
        score += Decimal("0.20")
        signals.append("repeated_characters")
    if _URL.search(name) or "@" in name:
        score += Decimal("0.30")
        signals.append("link_or_address_in_name")
    score = min(score, Decimal("1")).quantize(Decimal("0.001"))
    return SpamVerdict(score, tuple(signals))


def _actor() -> dict:
    return {"type": "visitor", "id": ""}


def _blocked(tenant, *, payload, verdict) -> None:
    """One ``blocked`` row for the hit that first crosses a limit in a window
    (decisions.md §4.8): a flood must not write the log without bound."""
    if not verdict.first_block:
        return
    row = log_received(
        command="lead.create", tenant=tenant, idempotency_key=None,
        actor=_actor(), trace_id=None, origin="", payload=payload,
    )
    log_conclude(row, outcome="blocked", problem={"rate_limited": verdict.rule})


def reject_spam_trap(tenant, *, listing, ip: "str | None") -> None:
    """A honeypot or too-fast submission. Creates nothing and tells the visitor
    nothing; a bot that keeps coming is refused like any other flood. Raises
    ``RateLimited`` only once the trap rule is exhausted."""
    require_autocommit()
    verdict = lead_limits.check_spam_trap(tenant, ip=ip, now=timezone.now())
    if not verdict.allowed:
        _blocked(tenant, payload={"listing_id": listing.public_id, "spam_trap": True}, verdict=verdict)
        raise RateLimited(verdict)


def create_lead(
    tenant,
    *,
    listing: Listing,
    kind: str,
    contact: dict,
    message: str,
    consent: dict,
    source_page: str = "",
    ip: "str | None",
) -> Lead:
    """The ``lead.create`` command. ``contact`` is ``{"name", "email",
    "phone_e164"}``; ``consent`` is ``{"contact_by_business": {"granted": bool}}``.

    There is no consent-version parameter: the server records whatever wording
    it is showing (spec §9.0). ``ip`` is required but nullable -- ``None`` for a
    caller with no client address, which skips the per-IP limits only.

    Raises ``ConsentRequired`` (consent missing or declined: a lead that cannot
    be answered is pointless), ``SchemaError`` (malformed, disabled, or an
    unpublished listing) and ``RateLimited``. A repeat of the same message to
    the same listing from the same address inside an hour returns the existing
    lead and queues nothing.
    """
    require_autocommit()

    try:
        # Types first: a non-string would otherwise raise AttributeError below
        # and leave the received row unconcluded (#257).
        normalize.require_mapping(contact, "contact")
        normalize.require_mapping(consent, "consent")
        for key, field in (
            ("email", "email"), ("name", "name"), ("phone_e164", "phone"),
        ):
            normalize.require_text(contact.get(key), field)
        email = normalize.email(contact.get("email")) or ""
        if not email:
            raise ValueError("email is required")
        name = (normalize.text(contact.get("name")) or "")[:NAME_MAX]
        if not name:
            raise ValueError("name is required")
        phone = contact.get("phone_e164") or ""
        if phone:
            try:
                phone = normalize.phone_e164(phone)
            except ValueError:
                # The command log is permanent: the message must not echo
                # the number back (#219).
                raise ValueError("phone is not a valid E.164 number") from None
        text = clean_message(message)
        if not MESSAGE_MIN <= len(text) <= MESSAGE_MAX:
            raise ValueError(
                f"the message must be {MESSAGE_MIN} to {MESSAGE_MAX} characters"
            )
        if kind not in Lead.Kind.values:
            raise ValueError(
                f"kind is not one of {', '.join(sorted(Lead.Kind.values))}"
            )
        source_page = (source_page or "")[:500]
        # Ids and the consent flags only: the contact details and the message
        # live on the lead row and its event, never in the permanent log (#219).
        payload = normalize.jsonable(
            {
                "listing_id": listing.public_id,
                "kind": kind,
                "consent": consent,
            }
        )
    except ValueError as exc:
        rejected = log_received(
            command="lead.create", tenant=tenant, idempotency_key=None,
            actor=_actor(), trace_id=None, origin="", payload=None,
        )
        log_conclude(rejected, outcome="rejected", problem={"payload": str(exc)})
        raise SchemaError([str(exc)]) from exc

    verdict = lead_limits.check_create(tenant, ip=ip, email=email, now=timezone.now())
    if not verdict.allowed:
        _blocked(tenant, payload=payload, verdict=verdict)
        raise RateLimited(verdict)

    row = log_received(
        command="lead.create", tenant=tenant, idempotency_key=None,
        actor=_actor(), trace_id=None, origin="", payload=payload,
    )
    try:
        lead, event_id, duplicate = _apply_create_lead(
            tenant, listing=listing, kind=kind, name=name, email=email,
            phone=phone, message=text, consent=consent, source_page=source_page,
            ip=ip,
        )
    except ConsentRequired as exc:
        log_conclude(row, outcome="rejected", problem={"missing_consent": exc.channel})
        raise
    except SchemaError as exc:
        log_conclude(row, outcome="rejected", problem={"errors": exc.errors})
        raise
    # Any other exception: the row keeps outcome=NULL (spec §11.2's "threw
    # mid-apply" record). Propagate.

    log_conclude(
        row,
        outcome="applied",
        result_event_id=event_id,
        problem={"duplicate_of": lead.public_id} if duplicate else None,
    )
    return lead


@transaction.atomic
def _apply_create_lead(
    tenant, *, listing, kind, name, email, phone, message, consent, source_page, ip
) -> "tuple[Lead, str, bool]":
    if not leads_enabled(tenant):
        raise SchemaError(["inquiries are not enabled for this directory"])
    listing = Listing.objects.select_for_update().get(pk=listing.pk)
    if (
        listing.visibility != Listing.Visibility.PUBLISHED
        or listing.status == Listing.Status.SUSPENDED
    ):
        raise SchemaError(["this listing is not accepting inquiries"])

    entry = consent.get(LEAD_CONSENT_CHANNEL)
    if not isinstance(entry, dict) or not entry.get("granted"):
        raise ConsentRequired(LEAD_CONSENT_CHANNEL)
    consent_text = get_lead_consent_text(tenant)

    now = timezone.now()
    existing = (
        Lead.objects.filter(
            listing=listing, email=email, message=message,
            created_at__gt=now - DUPLICATE_WINDOW,
        )
        .order_by("-id")
        .first()
    )
    if existing is not None:
        return existing, "", True

    verdict = spam_score(name=name, email=email, message=message)
    is_spam = verdict.score >= SPAM_THRESHOLD
    lead = Lead.objects.create(
        tenant=tenant, listing=listing, kind=kind, name=name, email=email,
        phone_e164=phone, message=message, spam_score=verdict.score,
        marked_spam=is_spam, source_page=source_page,
    )
    Consent.objects.create(
        tenant=tenant, lead=lead, channel=LEAD_CONSENT_CHANNEL, granted=True,
        granted_at=now, ip=storable_ip(ip), text_version=str(consent_text),
    )
    captured = emit(
        events.LEAD_CAPTURED,
        subject=lead.public_id,
        tenant=tenant,
        actor=_actor(),
        data={
            "lead": {
                "id": lead.public_id,
                "kind": lead.kind,
                "name": lead.name,
                "email": lead.email,
                "phone_e164": lead.phone_e164,
                "message": lead.message,
                "spam_score": float(verdict.score),
            },
            "listing_id": listing.public_id,
            "source_page": lead.source_page,
            "consent": {
                LEAD_CONSENT_CHANNEL: {
                    "granted": True,
                    "at": now.isoformat(),
                    "ip": storable_ip(ip),
                    "text_version": str(consent_text),
                }
            },
        },
    )
    if is_spam:
        emit(
            events.LEAD_MARKED_SPAM,
            subject=lead.public_id,
            tenant=tenant,
            actor={"type": "system", "id": ""},
            data={"lead_id": lead.public_id, "by": "system", "signals": list(verdict.signals)},
        )
    else:
        lead_notices.notify_for_lead(tenant, listing=listing, now=now)
    return lead, captured.event_id, False


# --- lead.mark_spam -----------------------------------------------------------


class LeadRefused(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


REFUSAL_TEXT = {
    "forbidden": "Your role does not allow marking leads as spam.",
    "already_marked": "This lead is already marked as spam.",
}


def mark_lead_spam(tenant, *, lead: Lead, operator) -> Lead:
    """The ``lead.mark_spam`` command, at moderator. One-way: a lead marked as
    spam stays marked, and is not delivered further."""
    require_autocommit()
    membership = _membership(tenant, operator)
    actor = _operator_actor(operator, membership)
    row = log_received(
        command="lead.mark_spam", tenant=tenant, idempotency_key=None,
        actor=actor, trace_id=None, origin="", payload={"lead_id": lead.public_id},
    )
    try:
        lead, event_id = _apply_mark_spam(
            tenant, lead=lead, operator=operator, membership=membership, actor=actor
        )
    except LeadRefused as exc:
        log_conclude(
            row,
            outcome="blocked" if exc.reason == "forbidden" else "rejected",
            problem={"reason": exc.reason},
        )
        raise
    log_conclude(row, outcome="applied", result_event_id=event_id)
    return lead


@transaction.atomic
def _apply_mark_spam(tenant, *, lead, operator, membership, actor):
    if membership is None or membership.role < MODERATE_ROLE:
        raise LeadRefused("forbidden")
    lead = Lead.objects.select_for_update().get(pk=lead.pk)
    if lead.marked_spam:
        raise LeadRefused("already_marked")
    lead.marked_spam = True
    lead.spam_marked_by = operator
    lead.save(update_fields=["marked_spam", "spam_marked_by"])
    event = emit(
        events.LEAD_MARKED_SPAM,
        subject=lead.public_id,
        tenant=tenant,
        actor=actor,
        data={"lead_id": lead.public_id, "by": operator.public_id, "signals": ["moderator"]},
    )
    return lead, event.event_id
