"""The bundled Stripe payment provider (decisions.md §4.11).

Implements the provider contract in ``osds.adapter_api`` -- and imports nothing
else from core. It is handed an ``AdapterContext`` (config, a secret resolver,
an HTTP client that reaches only ``api.stripe.com``, a clock), asks Stripe for
checkout sessions, subscription cancellation and customer-portal links, and
turns Stripe's webhooks into ``PaymentReport``s. It never writes a table, calls
a service or emits an event. stdlib only: no Stripe SDK.

Pricing is by Stripe Price id, one per purchasable tier, entered on the
settings page. Core's tier price is what the owner is shown, so before a
checkout is created the adapter reads the Price from Stripe and refuses if its
amount, currency or interval differs: a mismatch would charge an owner
something other than what they were offered.
"""

from __future__ import annotations

import json

from adapters.stripe import config as cfg
from adapters.stripe import events as stripe_events
from adapters.stripe import signature
from adapters.stripe.api import HOST, StripeClient, StripeNotFound, path_id
from osds.adapter_api import (
    CheckoutRequest,
    CheckoutResult,
    InboundRequest,
    InboundResult,
    PaymentError,
    SecretNotFound,
)


class StripeProvider:
    adapter_id = cfg.ADAPTER_ID
    egress_allowlist = cfg.EGRESS_ALLOWLIST

    # -- settings ---------------------------------------------------------------
    def settings_fields(self):
        return list(cfg.FIELDS)

    def available(self, ctx) -> bool:
        """Configured, not reachable: both secrets are present."""
        try:
            ctx.secret("secret_key")
            ctx.secret("webhook_secret")
        except SecretNotFound:
            return False
        return True

    # -- checkout ---------------------------------------------------------------
    def _verify_price(self, client: StripeClient, price_id: str, request: CheckoutRequest) -> None:
        price = client.get(f"prices/{path_id('price', price_id)}")
        recurring = price.get("recurring") or {}
        problems = []
        if not price.get("active", False):
            problems.append("inactive")
        if price.get("unit_amount") != request.price_minor:
            problems.append("amount")
        if (price.get("currency") or "").lower() != request.currency.lower():
            problems.append("currency")
        if recurring.get("interval") != request.interval or recurring.get("interval_count", 1) != 1:
            problems.append("interval")
        if problems:
            raise PaymentError(
                f"the Stripe price for {request.tier_key!r} does not match the tier: {', '.join(problems)}"
            )

    def create_checkout(self, ctx, request: CheckoutRequest) -> CheckoutResult:
        client = StripeClient(ctx)
        price_id = cfg.price_for(ctx.config, request.tier_key)
        self._verify_price(client, price_id, request)
        metadata = {
            "attempt_id": request.attempt_id,
            "listing_id": request.listing_id,
            "tier": request.tier_key,
        }
        params = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": request.success_url,
            "cancel_url": request.cancel_url,
            "client_reference_id": request.attempt_id,
            "customer_email": request.customer_email,
            "payment_method_collection": "always",  # a card up front (spec §6.7)
            "metadata": metadata,
            # Stripe copies this onto the subscription, so every later event
            # for it names the listing without another lookup.
            "subscription_data": {"metadata": metadata},
        }
        if request.trial_days:
            params["subscription_data"]["trial_period_days"] = request.trial_days
        session = client.post("checkout/sessions", params, idempotency_key=request.attempt_id)
        url = session.get("url")
        if not url or not session.get("id"):
            raise PaymentError("stripe did not return a checkout url")
        return CheckoutResult(url=url, external_ref=session["id"])

    # -- subscription -----------------------------------------------------------
    def cancel_subscription(self, ctx, payment_ref: dict, *, at_period_end: bool) -> None:
        client = StripeClient(ctx)
        sub = path_id("sub", (payment_ref or {}).get("external_id", ""))
        try:
            if at_period_end:
                client.post(f"subscriptions/{sub}", {"cancel_at_period_end": True})
            else:
                client.delete(f"subscriptions/{sub}")
        except StripeNotFound:
            return  # already gone: the outcome asked for

    def portal_url(self, ctx, payment_ref: dict, return_url: str) -> "str | None":
        customer = (payment_ref or {}).get("customer", "")
        if not customer:
            return None
        session = StripeClient(ctx).post(
            "billing_portal/sessions",
            {"customer": path_id("cus", customer), "return_url": return_url},
        )
        return session.get("url") or None

    # -- inbound ----------------------------------------------------------------
    def inbound(self, ctx, request: InboundRequest) -> InboundResult:
        if request.method != "POST":
            return InboundResult(405, "method not allowed")
        try:
            signature.verify(
                request.headers.get("stripe-signature", ""), request.body,
                ctx.secret("webhook_secret"), now=ctx.clock().timestamp(),
            )
        except signature.SignatureError:
            return InboundResult(400, "invalid signature")  # the reason is never sent back

        try:
            event = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return InboundResult(400, "malformed body")
        if (
            not isinstance(event, dict) or not event.get("id") or not event.get("type")
            or not isinstance((event.get("data") or {}).get("object"), dict)
        ):
            return InboundResult(400, "malformed event")

        try:
            reports = stripe_events.map_event(event, StripeClient(ctx), ctx.config)
        except PaymentError:
            return InboundResult(503, "lookup failed")  # Stripe retries
        return InboundResult(200, "ok", tuple(reports))
