"""Mapping Stripe events to ``PaymentReport``s (decisions.md §4.11).

The adapter says what happened; core decides what it means. Each handled event
yields at most one report, whose ``external_event_id`` is Stripe's *event* id --
Stripe's own advice is to track event ids, since two distinct events can
describe one thing and an event can be delivered twice.

========================================  ====================================
Stripe event                              Report
========================================  ====================================
``checkout.session.completed`` (a         ``checkout_completed`` -- with the
subscription session)                     subscription's period end, or its
                                          trial end
``invoice.paid``                          ``payment_succeeded`` (a $0 invoice
                                          that opens a trial is skipped: no
                                          money moved)
``invoice.payment_failed``                ``payment_failed``
``customer.subscription.updated``         ``subscription_canceled`` (cancel at
                                          period end switched on),
                                          ``payment_succeeded`` (switched off:
                                          the owner kept the plan), or
                                          ``subscription_changed`` (a price
                                          that is a configured tier)
``customer.subscription.deleted``         ``subscription_canceled``, ended now
``charge.refunded`` (fully refunded)      ``refunded``
anything else                             nothing; acknowledged
========================================  ====================================

Stripe's account API version decides an event's shape, and the shape moved
between versions (the invoice's subscription, a subscription's period end).
Both are read; ``subscription`` objects are only fetched when the event does
not carry what core needs.

Listing resolution rides on metadata the adapter itself put on the Checkout
Session and on the subscription (``attempt_id``, ``listing_id``, ``tier``), so
an event for a subscription this adapter did not create is not core's and is
ignored.
"""

from __future__ import annotations

from datetime import datetime, timezone

from adapters.stripe import config as cfg
from adapters.stripe.api import StripeClient, StripeNotFound, path_id
from osds.adapter_api import PaymentError, PaymentReport

ADAPTER = cfg.ADAPTER_ID


def _ts(value) -> "datetime | None":
    if value in (None, "", 0):
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _ref(subscription_id: str, customer_id: str) -> dict:
    ref = {"adapter": ADAPTER, "external_id": subscription_id or ""}
    if customer_id:
        ref["customer"] = customer_id
    return ref


def _sub_id(obj: dict) -> str:
    """An invoice's subscription id, in either API shape."""
    value = obj.get("subscription")
    if not value:
        value = ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
    return value.get("id", "") if isinstance(value, dict) else (value or "")


def _invoice_metadata(invoice: dict) -> dict:
    meta = (invoice.get("subscription_details") or {}).get("metadata")
    if not meta:
        meta = ((invoice.get("parent") or {}).get("subscription_details") or {}).get("metadata")
    return meta or {}


def _period_end(subscription: dict) -> "datetime | None":
    """A subscription's current period end: on the subscription in older API
    versions, on its items in newer ones."""
    value = subscription.get("current_period_end")
    if not value:
        ends = [i.get("current_period_end") for i in (subscription.get("items") or {}).get("data", [])]
        value = max((e for e in ends if e), default=None)
    return _ts(value)


def _invoice_period_end(invoice: dict) -> "datetime | None":
    ends = [((l.get("period") or {}).get("end")) for l in (invoice.get("lines") or {}).get("data", [])]
    return _ts(max((e for e in ends if e), default=None) or invoice.get("period_end"))


def _fetch_subscription(client: StripeClient, sub_id: str) -> dict:
    return client.get(f"subscriptions/{path_id('sub', sub_id)}")


def _meta_ids(meta: dict) -> "tuple[str, str]":
    return (meta.get("attempt_id", ""), meta.get("listing_id", ""))


# --- handlers -----------------------------------------------------------------


def _checkout_completed(event, client, config) -> "list[PaymentReport]":
    session = event["data"]["object"]
    if session.get("mode") != "subscription":
        return []
    meta = session.get("metadata") or {}
    attempt = session.get("client_reference_id") or meta.get("attempt_id", "")
    if not attempt:
        return []  # not a checkout this adapter started
    sub_id = session.get("subscription")
    sub_id = sub_id.get("id", "") if isinstance(sub_id, dict) else (sub_id or "")
    if not sub_id:
        return []
    customer = session.get("customer")
    customer = customer.get("id", "") if isinstance(customer, dict) else (customer or "")
    subscription = _fetch_subscription(client, sub_id)  # PaymentError -> retry
    ref = _ref(sub_id, customer)
    if subscription.get("status") == "trialing" and subscription.get("trial_end"):
        return [PaymentReport(
            kind="checkout_completed", external_event_id=event["id"],
            checkout_attempt_id=attempt, listing_id=meta.get("listing_id", ""),
            trial_ends_at=_ts(subscription["trial_end"]),
            period_end=_period_end(subscription), payment_ref=ref,
        )]
    period_end = _period_end(subscription)
    if period_end is None:
        raise PaymentError("the subscription has no period end")
    return [PaymentReport(
        kind="checkout_completed", external_event_id=event["id"],
        checkout_attempt_id=attempt, listing_id=meta.get("listing_id", ""),
        period_end=period_end, payment_ref=ref,
    )]


def _context_for_invoice(invoice, client) -> "tuple[str, str, str]":
    """``(subscription id, attempt id, listing id)`` for an invoice, reading the
    subscription only when the invoice does not carry the metadata."""
    sub_id = _sub_id(invoice)
    attempt, listing = _meta_ids(_invoice_metadata(invoice))
    if sub_id and not (attempt or listing):
        try:
            attempt, listing = _meta_ids(_fetch_subscription(client, sub_id).get("metadata") or {})
        except StripeNotFound:
            pass
    return sub_id, attempt, listing


def _invoice_paid(event, client, config) -> "list[PaymentReport]":
    invoice = event["data"]["object"]
    sub_id, attempt, listing = _context_for_invoice(invoice, client)
    if not (attempt or listing):
        return []
    amount = invoice.get("amount_paid")
    if amount == 0 and invoice.get("billing_reason") == "subscription_create":
        return []  # the $0 invoice that opens a trial: no money moved, the trial is not "converted"
    period_end = _invoice_period_end(invoice)
    if period_end is None:
        raise PaymentError("the invoice has no period end")
    customer = invoice.get("customer")
    customer = customer.get("id", "") if isinstance(customer, dict) else (customer or "")
    return [PaymentReport(
        kind="payment_succeeded", external_event_id=event["id"],
        checkout_attempt_id=attempt, listing_id=listing, period_end=period_end,
        payment_ref=_ref(sub_id, customer), amount_minor=amount,
        currency=(invoice.get("currency") or "").upper(), invoice_ref=invoice.get("id", ""),
    )]


def _invoice_failed(event, client, config) -> "list[PaymentReport]":
    invoice = event["data"]["object"]
    sub_id, attempt, listing = _context_for_invoice(invoice, client)
    if not (attempt or listing):
        return []
    return [PaymentReport(
        kind="payment_failed", external_event_id=event["id"],
        checkout_attempt_id=attempt, listing_id=listing,
        failure_code="invoice_payment_failed", attempt=invoice.get("attempt_count"),
        next_retry_at=_ts(invoice.get("next_payment_attempt")),
        payment_ref=_ref(sub_id, ""),
    )]


def _subscription_updated(event, client, config) -> "list[PaymentReport]":
    sub = event["data"]["object"]
    previous = event["data"].get("previous_attributes") or {}
    attempt, listing = _meta_ids(sub.get("metadata") or {})
    if not (attempt or listing):
        return []
    customer = sub.get("customer")
    customer = customer.get("id", "") if isinstance(customer, dict) else (customer or "")
    ref = _ref(sub.get("id", ""), customer)
    base = dict(external_event_id=event["id"], checkout_attempt_id=attempt, listing_id=listing,
                payment_ref=ref)

    if "cancel_at_period_end" in previous:
        if sub.get("cancel_at_period_end"):
            details = sub.get("cancellation_details") or {}
            return [PaymentReport(
                kind="subscription_canceled", at_period_end=True, canceled_by="owner",
                reason=(details.get("reason") or "")[:100], **base,
            )]
        # Switched back on: the owner kept the plan. Core only returns a
        # cancelled listing to active through a successful payment (G2).
        period_end = _period_end(sub)
        if period_end is None:
            raise PaymentError("the subscription has no period end")
        return [PaymentReport(kind="payment_succeeded", period_end=period_end, **base)]

    if "items" in previous:
        items = (sub.get("items") or {}).get("data", [])
        price = ((items[0].get("price") or {}).get("id", "")) if items else ""
        tier = cfg.tier_for_price(config, price)
        period_end = _period_end(sub)
        if tier and period_end is not None:
            return [PaymentReport(kind="subscription_changed", tier_key=tier,
                                  period_end=period_end, **base)]
    return []


def _subscription_deleted(event, client, config) -> "list[PaymentReport]":
    sub = event["data"]["object"]
    attempt, listing = _meta_ids(sub.get("metadata") or {})
    if not (attempt or listing):
        return []
    details = sub.get("cancellation_details") or {}
    return [PaymentReport(
        kind="subscription_canceled", external_event_id=event["id"],
        checkout_attempt_id=attempt, listing_id=listing, at_period_end=False,
        canceled_by="provider", reason=(details.get("reason") or "")[:100],
        payment_ref=_ref(sub.get("id", ""), ""),
    )]


def _charge_refunded(event, client, config) -> "list[PaymentReport]":
    charge = event["data"]["object"]
    if not charge.get("refunded"):
        return []  # a partial refund does not end the plan
    customer = charge.get("customer")
    customer = customer.get("id", "") if isinstance(customer, dict) else (customer or "")
    if not customer:
        return []
    found = client.get("subscriptions", {"customer": customer, "status": "all", "limit": 10})
    for sub in found.get("data", []):
        attempt, listing = _meta_ids(sub.get("metadata") or {})
        if attempt or listing:
            return [PaymentReport(
                kind="refunded", external_event_id=event["id"],
                checkout_attempt_id=attempt, listing_id=listing,
                amount_minor=charge.get("amount_refunded"),
                currency=(charge.get("currency") or "").upper(), reason="refunded",
                payment_ref=_ref(sub.get("id", ""), customer),
            )]
    return []


HANDLERS = {
    "checkout.session.completed": _checkout_completed,
    "invoice.paid": _invoice_paid,
    "invoice.payment_failed": _invoice_failed,
    "customer.subscription.updated": _subscription_updated,
    "customer.subscription.deleted": _subscription_deleted,
    "charge.refunded": _charge_refunded,
}


def map_event(event: dict, client: StripeClient, config: dict) -> "list[PaymentReport]":
    handler = HANDLERS.get(event.get("type", ""))
    return handler(event, client, config) if handler else []
