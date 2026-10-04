"""What core and an adapter say to each other (decisions.md §4.11, §4.12).

An adapter imports this module and ``osds.adapters`` and nothing else from
core: it is handed an ``AdapterContext`` (its config, a way to read its secrets,
an HTTP client that only reaches its declared hosts, a clock) and returns plain
data. It never calls a service function, never writes a table and never emits
an event; core applies what it reports (spec §8.3). That is what #215 asks of
the SMTP sender too, and what the new adapters do from the start.

Provider contract for the ``payments.checkout`` and ``payments.subscription``
capabilities. One provider object is registered under both names and exposes::

    adapter_id: str
    egress_allowlist: tuple[str, ...]          # hosts it may call
    def available(self, ctx) -> bool           # configured, not reachable
    def settings_fields(self) -> list[SettingField]
    def create_checkout(self, ctx, request: CheckoutRequest) -> CheckoutResult
    def cancel_subscription(self, ctx, payment_ref: dict, *, at_period_end: bool) -> None
    def portal_url(self, ctx, payment_ref: dict, return_url: str) -> "str | None"
    def inbound(self, ctx, request: InboundRequest) -> InboundResult

Money is integer minor units plus an ISO 4217 code, never a float.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime


class PaymentError(Exception):
    """The provider refused or failed. The message is for the log and never
    carries a secret or a card detail."""


class SecretNotFound(Exception):
    """The adapter asked for a secret nobody configured."""


class EgressDenied(Exception):
    """The adapter tried to reach a host it did not declare (spec §8.3)."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: "dict[str, str]" = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterContext:
    """The minimal context an adapter is given (spec §8)."""

    tenant_id: str  # the tenant's public id
    tenant_slug: str
    tenant_domain: "str | None"
    config: "dict[str, object]"
    secret: "Callable[[str], str]"  # raises SecretNotFound
    http: "Callable[..., HttpResponse]"  # fetch(method, url, *, headers, body, timeout)
    clock: "Callable[[], datetime]"
    # An ``osds.adapter_logging.ScrubbingLogger``: every secret the adapter has
    # resolved is removed from anything it logs (spec §8.1, #179).
    logger: object = None


# The scopes an adapter may ask for (spec §8). Granting one is the tenant
# admin's decision, never the adapter's (invariant 8): the label is what the
# admin reads before turning it on.
SCOPE_LABELS = {
    "pii:contact": "Contact details: names, email addresses, phone numbers, IP "
    "addresses, and fields the operator keeps private",
    "pii:message": "Free text that people wrote: inquiry messages, review notes "
    "and reasons",
}


@dataclass(frozen=True)
class SettingField:
    """One field of an adapter's settings page. Core renders the form from
    these, so core never names the vendor; ``secret`` fields go to the secret
    store and are never shown again. ``kind`` is ``"text"`` or ``"bool"``."""

    key: str
    label: str
    secret: bool = False
    required: bool = False
    help_text: str = ""
    kind: str = "text"
    # A regular expression the whole value must match, checked by core before
    # anything is saved. A failure names the field and never echoes the value
    # (it may be a secret).
    pattern: str = ""


@dataclass(frozen=True)
class MailMessage:
    """One outbound email, as the ``email.send`` provider sees it: plain data,
    never the ORM row (#215). ``kind`` names what caused it and carries no
    personal data."""

    message_id: str
    kind: str
    to_address: str
    subject: str
    body_text: str


@dataclass(frozen=True)
class CheckoutRequest:
    attempt_id: str  # core's ``chk_`` id; the provider echoes it back
    listing_id: str
    listing_name: str
    tier_key: str
    tier_name: str
    price_minor: int
    currency: str
    interval: str  # "month" | "year"
    trial_days: "int | None"
    customer_email: str
    success_url: str
    cancel_url: str


@dataclass(frozen=True)
class CheckoutResult:
    url: str
    external_ref: str = ""


# What a provider can report. ``kind`` is one of REPORT_KINDS.
REPORT_KINDS = (
    "checkout_completed",
    "payment_succeeded",
    "payment_failed",
    "refunded",
    "subscription_changed",
    "subscription_canceled",
)


@dataclass(frozen=True)
class PaymentReport:
    kind: str
    external_event_id: str
    # How core finds the listing: the checkout attempt it started, or the
    # listing id the provider stored on the subscription.
    checkout_attempt_id: str = ""
    listing_id: str = ""
    tier_key: str = ""
    period_end: "datetime | None" = None
    trial_ends_at: "datetime | None" = None
    payment_ref: "dict[str, str]" = field(default_factory=dict)
    amount_minor: "int | None" = None
    currency: str = ""
    invoice_ref: str = ""
    failure_code: str = ""
    attempt: "int | None" = None
    next_retry_at: "datetime | None" = None
    at_period_end: bool = True
    canceled_by: str = ""
    reason: str = ""


@dataclass(frozen=True)
class InboundRequest:
    method: str
    headers: "dict[str, str]"  # lower-cased names
    body: bytes  # the raw body, exactly as received: signatures are over it


@dataclass(frozen=True)
class InboundResult:
    status: int = 200
    body: str = ""
    reports: "tuple[PaymentReport, ...]" = ()
