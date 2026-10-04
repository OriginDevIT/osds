"""The bundled ``webhook`` adapter (spec §8.6, decisions.md §4.12).

POSTs a tenant's events to one URL the operator chose -- the universal escape
hatch. It imports ``osds.adapter_api`` and ``osds.adapters`` and nothing else
from the project; core hands it an ``AdapterContext`` and an envelope that
``audit.redaction`` has already stripped of whatever the tenant has not granted.

* **One endpoint per tenant.** The URL is a *secret* (``webhook_url``): hook URLs
  usually embed a token, and non-secret configuration is written into
  ``tenant.settings_changed`` events. Configuration holds only the host, for
  display and for the egress check, the event patterns, and the scope grants.
  The signing secret is ``webhook_signing_secret``.
* **Delivery** goes through the outbox drain: at-least-once, ordered per
  subject, jittered backoff, 12 attempts, then the tenant's dead letters. This
  module only maps what the endpoint answered to a ``Result``.
* **Egress** is ``ctx.http``: the configured host only, public addresses only
  (unless the deployment opted in to private ones), no redirects.
* **Nothing the endpoint says is recorded.** ``last_error`` is a status code or
  an error class; a response body can echo the payload.
"""

from __future__ import annotations

import json
import re
from datetime import timezone as dt_timezone
from urllib.parse import urlsplit

from osds.adapter_api import EgressDenied, SecretNotFound, SettingField
from osds.adapters import Result, matches

from adapters.webhook import signing

ADAPTER_ID = "webhook"
MAX_BODY_BYTES = 1024 * 1024
HTTP_TIMEOUT = 25
_PATTERN = re.compile(r"^[a-z0-9_.*]+$")


def parse_patterns(raw) -> "list[str]":
    """``"claim.*, lead.captured"`` -> ``["claim.*", "lead.captured"]``."""
    return [p for p in re.split(r"[\s,]+", str(raw or "").strip().lower()) if p]


class WebhookAdapter:
    id = ADAPTER_ID
    adapter_id = ADAPTER_ID
    scopes = frozenset({"pii:contact", "pii:message"})
    subscribes = ("*",)

    # -- the fan-out filter ----------------------------------------------------
    def wants(self, event_type: str, config) -> bool:
        """A tenant with no enabled endpoint, or no matching pattern, gets no
        delivery rows. ``tenant.*`` events have no tenant to configure this."""
        if not config or event_type.startswith("tenant."):
            return False
        if config.get("enabled") is not True:
            return False
        return matches(parse_patterns(config.get("events")), event_type)

    # -- egress ----------------------------------------------------------------
    def egress_allowlist_for(self, config) -> "tuple[str, ...]":
        host = (config.get("host") or "").strip().lower()
        return (host,) if host else ()

    # -- settings page ---------------------------------------------------------
    def settings_fields(self) -> "list[SettingField]":
        return [
            SettingField("enabled", "Send events to the endpoint", kind="bool"),
            SettingField(
                "url", "Endpoint URL", secret=True,
                help_text="https://... It is stored encrypted and never shown again: "
                "hook URLs usually contain a token.",
            ),
            SettingField(
                "events", "Event types",
                help_text="Comma-separated patterns: claim.*, lead.captured, or * for all. "
                "Empty sends nothing.",
            ),
        ]

    rotatable_secrets = ("signing_secret",)

    def generate_secret(self, key: str) -> str:
        import secrets

        return "whsec_" + secrets.token_hex(32)

    def validate_settings(self, values: dict, *, stored) -> "list[str]":
        """Errors in a settings submission, in plain words. ``values`` carries the
        non-secret fields and, for a new or removed URL, ``url``; ``stored`` is the
        set of this adapter's secrets that will exist after the save."""
        errors: "list[str]" = []
        for pattern in parse_patterns(values.get("events")):
            if not _PATTERN.match(pattern) or pattern.startswith("tenant."):
                errors.append(f"{pattern!r} is not an event pattern you can subscribe to.")
        url = (values.get("url") or "").strip()
        if url:
            parts = urlsplit(url)
            if parts.scheme not in ("https", "http") or not parts.hostname:
                errors.append("The endpoint URL must start with https:// and name a host.")
            elif parts.username or parts.password:
                errors.append("Put credentials in the signing secret or the path, not before the host.")
        if values.get("enabled") and not (url or "url" in stored):
            errors.append("Enter an endpoint URL before turning the endpoint on.")
        return errors

    def derive_config(self, values: dict) -> dict:
        """Non-secret configuration worked out from the submission: the host,
        which is what core shows and what the egress check is held to."""
        if "url" not in values:
            return {}  # the stored URL is unchanged, and so is its host
        url = (values["url"] or "").strip()
        return {"host": (urlsplit(url).hostname or "").lower() if url else ""}

    # -- delivery ----------------------------------------------------------------
    def handle(self, envelope: dict, ctx) -> Result:
        if ctx is None:
            return Result.skipped("no tenant")
        config = ctx.config
        if config.get("enabled") is not True:
            return Result.skipped("the endpoint is turned off")
        if envelope.get("origin") == ADAPTER_ID:
            return Result.skipped("originated here")
        if not matches(parse_patterns(config.get("events")), envelope["type"]):
            return Result.skipped("filtered out")
        try:
            url = ctx.secret("url")
            key = ctx.secret("signing_secret")
        except SecretNotFound:
            return Result.skipped("not configured")
        return self._post(ctx, url, key, envelope)

    def _post(self, ctx, url: str, key: str, envelope: dict) -> Result:
        body = json.dumps(
            envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            ctx.logger.warning("event %s is too large to send", envelope["id"])
            return Result.failed("PayloadTooLarge", permanent=True)
        timestamp = int(ctx.clock().astimezone(dt_timezone.utc).timestamp())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "OSDS-Webhook/1",
            "X-OSDS-Event-Id": envelope["id"],
            "X-OSDS-Event-Type": envelope["type"],
            "X-OSDS-Timestamp": str(timestamp),
            "X-OSDS-Signature": signing.header(key, timestamp, body),
        }
        try:
            response = ctx.http("POST", url, headers=headers, body=body, timeout=HTTP_TIMEOUT)
        except EgressDenied:
            # A refusal does not heal by itself: dead-letter it for the admin to
            # fix and replay rather than burn twelve attempts.
            ctx.logger.warning("event %s was refused by the egress policy", envelope["id"])
            return Result.failed("EgressDenied", permanent=True)
        except Exception as exc:  # connect, TLS, timeout, reset: all retry
            ctx.logger.warning("event %s failed: %s", envelope["id"], type(exc).__name__)
            return Result.failed(type(exc).__name__)
        return self._classify(ctx, envelope, response)

    def _classify(self, ctx, envelope: dict, response) -> Result:
        status = response.status
        ctx.logger.info("event %s %s -> http %s", envelope["id"], envelope["type"], status)
        if 200 <= status < 300:
            return Result.ok(f"http {status}")
        if status == 409:  # the receiver already has it (spec §8.4)
            return Result.ok("http 409 duplicate")
        if status == 429:
            return Result.retry(_retry_after_ms(response.headers), "http 429")
        if status == 410:  # gone: the endpoint says it will never take events
            return Result.failed("http 410", permanent=True)
        if 300 <= status < 400:
            return Result.failed(f"http {status} (redirects are not followed)")
        return Result.failed(f"http {status}")

    # -- the admin's "send a test" ---------------------------------------------------
    def send_test(self, ctx) -> "tuple[bool, str]":
        try:
            url = ctx.secret("url")
            key = ctx.secret("signing_secret")
        except SecretNotFound:
            return False, "Set the endpoint URL and generate a signing secret first."
        now = ctx.clock().astimezone(dt_timezone.utc)
        event_id = f"test_{int(now.timestamp())}"
        envelope = {
            "id": event_id,
            "type": "webhook.test",
            "version": 1,
            "occurred_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "subject": ctx.tenant_id,
            "actor": {"type": "admin", "id": ""},
            "origin": None,
            "trace_id": event_id,
            "data": {"message": "This is a test event from OSDS."},
            "redacted": [],
            "tenant": {"id": ctx.tenant_id, "slug": ctx.tenant_slug, "domain": ctx.tenant_domain},
        }
        result = self._post(ctx, url, key, envelope)
        if result.status == "ok":
            return True, result.reason
        return False, result.reason or result.status


def _retry_after_ms(headers) -> int:
    """``Retry-After`` as milliseconds, or 0 (no hint) if it is absent or a date."""
    try:
        seconds = int((headers or {}).get("retry-after", ""))
    except (TypeError, ValueError):
        return 0
    return max(0, seconds) * 1000
