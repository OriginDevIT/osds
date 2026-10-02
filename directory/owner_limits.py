"""The owner sign-in rate limits (#210, decisions.md §4.8, §4.9).

Core-fixed, like ``directory.claim_limits``. Only the per-IP limit lives here:
the per-account limit is derived from the ``OwnerSignInToken`` rows themselves
(``directory.owner_auth``), because it is a limit on mail sent to one real
owner and the rows already say exactly that.
"""

from __future__ import annotations

from datetime import timedelta

from audit.ratelimit import Rule, Verdict, check

REQUEST_IP = (
    Rule("owner.signin.ip.15m", 10, timedelta(minutes=15)),
    Rule("owner.signin.ip.24h", 40, timedelta(hours=24)),
)


def check_request(tenant, *, ip: "str | None", now) -> Verdict:
    return check(tenant, checks=[("ip", ip, REQUEST_IP)], now=now)
