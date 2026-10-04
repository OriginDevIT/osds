"""The Stripe adapter's settings: what the operator enters, and how it is read.

Core renders the settings page from ``settings_fields()``; the two secrets go
to the secret store and the price ids are configuration. ``pattern`` is checked
by core before anything is saved (never echoing a secret).
"""

from __future__ import annotations

import re

from osds.adapter_api import PaymentError, SettingField

ADAPTER_ID = "stripe"
EGRESS_ALLOWLIST = ("api.stripe.com",)

_PAIR = r"\s*[a-z0-9][a-z0-9-]*\s*=\s*price_[A-Za-z0-9]+\s*"
PRICE_IDS_PATTERN = rf"^{_PAIR}(,{_PAIR})*$"

FIELDS = (
    SettingField(
        "secret_key", "Secret key", secret=True, required=True,
        help_text="Your Stripe secret or restricted key (sk_… or rk_…). Test keys work for test mode.",
        pattern=r"^(sk|rk)_(test|live)_[A-Za-z0-9]+$",
    ),
    SettingField(
        "webhook_secret", "Webhook signing secret", secret=True, required=True,
        help_text="The whsec_… secret of the endpoint you create for this directory's webhook URL.",
        pattern=r"^whsec_[A-Za-z0-9]+$",
    ),
    SettingField(
        "price_ids", "Price ids",
        help_text=(
            "One Stripe price id per purchasable tier, as tier-key=price_…, comma separated: "
            "featured=price_1ABC, verified=price_1DEF. Each price must match the tier's "
            "amount, currency and interval."
        ),
        pattern=PRICE_IDS_PATTERN,
    ),
)


def parse_price_ids(raw) -> "dict[str, str]":
    """``{tier_key: price_id}`` from the setting's text."""
    out: "dict[str, str]" = {}
    for element in str(raw or "").split(","):
        key, sep, value = element.partition("=")
        if sep and key.strip() and value.strip():
            out[key.strip()] = value.strip()
    return out


def price_for(config: dict, tier_key: str) -> str:
    price = parse_price_ids(config.get("price_ids")).get(tier_key, "")
    if not re.fullmatch(r"price_[A-Za-z0-9]+", price):
        raise PaymentError(f"no price id is configured for the {tier_key!r} tier")
    return price


def tier_for_price(config: dict, price_id: str) -> str:
    """The tier key a price id is configured for, or ``""``."""
    for key, value in parse_price_ids(config.get("price_ids")).items():
        if value == price_id:
            return key
    return ""
