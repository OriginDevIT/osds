"""The adapter registry seam.

The worker reads subscribers from here and from nowhere else. Core never
imports adapter code (CLAUDE.md invariant 1): an adapter package calls
``register()`` at import time, and the worker only ever sees the ``Subscriber``
protocol and the ``Result`` it returns.

A subscriber may declare more than ``id`` and ``handle``:

* ``subscribes`` -- patterns of event types it wants (``"claim.*"``,
  ``"lead.captured"``, ``"*"``); absent means everything. ``subscribers_for``
  applies them.
* ``scopes`` -- what it asks to see (``pii:contact``, ``pii:message``). Asking is
  not receiving: the tenant's admin grants a scope, and the drain redacts
  everything it was not granted before ``handle`` is called (invariant 8,
  ``audit.redaction``). A subscriber that declares none sees none.
* ``wants(event_type, config)`` -- a per-tenant filter the fan-out consults, with
  the adapter's own config (``None`` for a ``tenant.*`` event), so a tenant that
  has not configured the adapter gets no delivery rows at all.
* ``config_key`` and ``egress_allowlist_for(config)`` -- see
  ``osds.adapter_context``.

Beside the event-subscriber registry sits a second one, for capabilities
(decisions.md §4.3): a capability is a named ability -- ``email.send`` today
-- resolved through ``capability_provider(name)`` to at most one provider. An
adapter package registers its provider from its ``AppConfig.ready()`` (the
``adapters.smtp`` app registers ``email.send``), never at import time, so the
registration happens exactly once regardless of how many modules import the
package. A second registration for the same name is a configuration error and
raises, unlike ``register()`` above, which is idempotent by design -- there is
supposed to be at most one provider per capability, never a list to
de-duplicate.
"""

from __future__ import annotations

import contextlib
import fnmatch
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Result:
    """A subscriber's report on one delivery attempt (spec §8 ``HandleResult``).

    ``status`` is ``"ok"`` | ``"skipped"`` | ``"retry"`` | ``"failed"``.
    ``retry_after_ms`` is the subscriber's requested backoff; the drain still
    clamps it to the 1s..1h window (§8.2). ``permanent`` on a ``"failed"``
    result skips the remaining retries.
    """

    status: str
    reason: str = ""
    retry_after_ms: "int | None" = None
    permanent: bool = False

    @classmethod
    def ok(cls, note: str = "") -> "Result":
        return cls("ok", note)

    @classmethod
    def skipped(cls, reason: str) -> "Result":
        return cls("skipped", reason)

    @classmethod
    def retry(cls, after_ms: int, reason: str = "") -> "Result":
        return cls("retry", reason, retry_after_ms=after_ms)

    @classmethod
    def failed(cls, reason: str, *, permanent: bool = False) -> "Result":
        return cls("failed", reason, permanent=permanent)


@runtime_checkable
class Subscriber(Protocol):
    """What the worker sees. An adapter implements this; the worker never
    imports the implementation, only calls ``handle`` with a wire envelope
    (``audit.envelope.to_wire``) that ``audit.redaction`` has already stripped
    of whatever the tenant has not granted, and an ``AdapterContext`` for the
    delivery's tenant (``None`` for a ``tenant.*`` event, which has none)."""

    id: str

    def handle(self, envelope: dict, ctx) -> Result: ...


_REGISTRY: "list[Subscriber]" = []


def register(subscriber: Subscriber) -> None:
    """Add a subscriber. Called by an adapter package at import time.
    Idempotent on ``id``."""
    if any(s.id == subscriber.id for s in _REGISTRY):
        return
    _REGISTRY.append(subscriber)


def matches(patterns, event_type: str) -> bool:
    """Whether ``event_type`` matches any of ``patterns`` (``fnmatch`` globs:
    ``*`` is everything, ``claim.*`` a namespace, a bare name itself)."""
    return any(fnmatch.fnmatchcase(event_type, p) for p in patterns)


def subscribers_for(event_type: str) -> "list[Subscriber]":
    """Subscribers whose ``subscribes`` patterns match ``event_type`` (all of
    them, for one that declares none)."""
    return [
        s
        for s in _REGISTRY
        if matches(getattr(s, "subscribes", None) or ("*",), event_type)
    ]


def subscriber_by_id(adapter_id: str) -> "Subscriber | None":
    """The registered subscriber with this id, or ``None``. The settings and
    deliveries pages find an adapter here and never import one."""
    for subscriber in _REGISTRY:
        if subscriber.id == adapter_id:
            return subscriber
    return None


def all_subscribers() -> "list[Subscriber]":
    return list(_REGISTRY)


@contextlib.contextmanager
def override_subscribers(
    *subscribers: Subscriber,
) -> "Iterator[list[Subscriber]]":
    """Install exactly ``subscribers`` for the duration of the block, then
    restore whatever was registered before.

    The registry is module state; a test that appends to ``_REGISTRY`` and
    trusts a later test to have cleared it is relying on isolation by
    convention. This is the supported seam: a ``with`` block, or
    ``self.enterContext(override_subscribers(...))`` in ``setUp``. The yielded
    list is live -- append to it or slice-assign it within the block.
    """
    global _REGISTRY
    saved, _REGISTRY = _REGISTRY, list(subscribers)
    try:
        yield _REGISTRY
    finally:
        _REGISTRY = saved


class CapabilityUnconfigured(Exception):
    """Raised by a capability provider when it is registered but lacks the
    configuration -- secrets, per-tenant settings -- needed to act.

    The caller must not treat this as a delivery failure: decisions.md §4.3
    says unavailable mail (no provider, or empty SMTP settings) leaves the
    row pending with no attempt consumed and no backoff applied. Raising
    this, rather than returning a ``Result``, keeps that "not a failure"
    distinction impossible to blur with an ordinary ``Result.failed()``.
    """


_CAPABILITIES: "dict[str, object]" = {}


def register_capability(name: str, provider: object) -> None:
    """Register ``provider`` for capability ``name``. Called by an adapter
    package's ``AppConfig.ready()``. Raises if ``name`` already has a
    provider -- one provider per capability, never a second silently
    replacing the first."""
    if name in _CAPABILITIES:
        raise ValueError(f"capability {name!r} already has a registered provider")
    _CAPABILITIES[name] = provider


def capability_provider(name: str) -> "object | None":
    """The registered provider for ``name``, or ``None`` if nothing is
    registered. ``None`` is an expected, valid state -- a fresh install with
    no adapter configured yet -- not an error."""
    return _CAPABILITIES.get(name)


def adapter_by_id(adapter_id: str) -> "object | None":
    """The registered provider whose ``adapter_id`` is ``adapter_id``, or
    ``None``. The inbound route resolves an adapter by the id in its URL; core
    never imports the adapter, it only finds what registered itself."""
    for provider in _CAPABILITIES.values():
        if getattr(provider, "adapter_id", None) == adapter_id:
            return provider
    return None


def email_available(tenant) -> bool:
    """Whether ``email.send`` is configured for ``tenant`` (decisions.md §4.5):
    configured, not reachable. False with no provider registered, or with a
    provider that does not expose ``available`` -- fail closed, so an
    unmarked provider never tells a claimant a code is coming. Core asks
    here and never reads the provider's own settings."""
    from osds.adapter_context import build_context

    provider = capability_provider("email.send")
    available = getattr(provider, "available", None)
    if available is None:
        return False
    return bool(available(build_context(tenant, provider)))


@contextlib.contextmanager
def override_capability(
    providers: "dict[str, object]",
) -> "Iterator[dict[str, object]]":
    """Install exactly ``providers`` for the duration of the block, then
    restore whatever was registered before. Mirrors ``override_subscribers``
    -- the supported seam for a test: a ``with`` block, or
    ``self.enterContext(override_capability({...}))`` in ``setUp``. Pass
    ``{}`` to simulate no provider registered at all.
    """
    global _CAPABILITIES
    saved, _CAPABILITIES = _CAPABILITIES, dict(providers)
    try:
        yield _CAPABILITIES
    finally:
        _CAPABILITIES = saved
