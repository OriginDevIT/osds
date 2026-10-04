"""The one shared ``email.send`` stub for tests (decisions.md §4.5).

Claim tests that need mail to be available enter ``email_send_stub()`` from
``setUp`` with ``self.enterContext(...)``, instead of each test building its
own ``override_capability``. ``email_send_stub(available=False)`` is the
unconfigured state.
"""

from __future__ import annotations

from osds.adapters import Result, override_capability


class StubEmailProvider:
    adapter_id = "stub"

    def __init__(self, available: bool = True):
        self._available = available
        self.sent: list = []

    def available(self, ctx) -> bool:
        return self._available

    def send(self, ctx, message) -> Result:
        self.sent.append(message)
        return Result.ok()


def email_send_stub(available: bool = True):
    """Context manager installing a ``StubEmailProvider`` as ``email.send``
    and yielding it."""
    return _Stub(StubEmailProvider(available))


class _Stub:
    def __init__(self, provider: StubEmailProvider):
        self.provider = provider
        self._cm = override_capability({"email.send": provider})

    def __enter__(self) -> StubEmailProvider:
        self._cm.__enter__()
        return self.provider

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)
