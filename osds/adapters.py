"""The adapter registry seam.

The worker reads subscribers from here and from nowhere else. Core never
imports adapter code (CLAUDE.md invariant 1): an adapter package calls
``register()`` at import time, and the worker only ever sees the ``Subscriber``
protocol and the ``Result`` it returns.

The registry is empty until the SMTP and webhook adapters land (mvp-plan
block 5). Pattern matching of an event type against an adapter's ``subscribes``
list lands with the drain, so for now ``subscribers_for`` returns whatever is
registered -- which is nothing.
"""

from __future__ import annotations

import contextlib
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
    (``audit.envelope.to_wire``)."""

    id: str

    def handle(self, envelope: dict) -> Result: ...


_REGISTRY: "list[Subscriber]" = []


def register(subscriber: Subscriber) -> None:
    """Add a subscriber. Called by an adapter package at import time.
    Idempotent on ``id``."""
    if any(s.id == subscriber.id for s in _REGISTRY):
        return
    _REGISTRY.append(subscriber)


def subscribers_for(event_type: str) -> "list[Subscriber]":
    """Subscribers that want ``event_type``. Empty until an adapter registers
    (mvp-plan block 5); ``subscribes`` pattern matching lands with the drain."""
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
