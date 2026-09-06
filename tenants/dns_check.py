"""Domain-verification check.

Verification is done over HTTP, not DNS records: request
``http://<domain>/.well-known/osds-challenge`` and compare the body to the
tenant's challenge token. A match proves the domain resolves *and* that it
reaches this installation -- DNS and reachability together. The recorded
method is ``"http"``.

The call is tightly time-boxed so it is safe to run inside the wizard request.
Repeated re-checks after propagation delays belong on the worker tick (later
PR).
"""

from __future__ import annotations

import urllib.error
import urllib.request

CHALLENGE_PATH = "/.well-known/osds-challenge"


def check_domain_http(
    domain: str, expected_token: str, *, timeout: float = 3.0
) -> tuple[bool, str]:
    """Return ``(ok, detail)``. ``ok`` is True only when the challenge endpoint
    returns exactly ``expected_token``."""
    domain = (domain or "").strip().rstrip(".").lower()
    expected = (expected_token or "").strip()
    if not domain or not expected:
        return False, "no domain or challenge token to check"

    url = f"http://{domain}{CHALLENGE_PATH}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "osds-setup"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", "replace").strip()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"could not reach {url}: {exc}"

    if body == expected:
        return True, "verified over HTTP"
    return (
        False,
        f"challenge mismatch at {url}: expected {expected!r}, saw {body[:64]!r}",
    )
