"""The lead-capture rate limits (#210, decisions.md §4.8 and §4.10).

Core-fixed, like the claim limits, and scoped per tenant by
``audit.ratelimit``. The constants are read at call time so a test that needs a
different limit patches the constant in its own module.

The business is protected from a flood by the notice throttle in
``directory.lead_notices``, not by a per-listing capture cap: a cap would let an
attacker switch one listing's form off.
"""

from __future__ import annotations

from datetime import timedelta

from audit.ratelimit import Rule, Verdict, check

CREATE_IP = (
    Rule("lead.create.ip.10m", 6, timedelta(minutes=10)),
    Rule("lead.create.ip.24h", 30, timedelta(hours=24)),
)
CREATE_EMAIL = (Rule("lead.create.email.24h", 10, timedelta(hours=24)),)
# A honeypot or too-fast hit: the visitor gets the normal confirmation and
# nothing is created, but a bot that keeps hitting it is refused like any other.
SPAM_TRAP_IP = (Rule("lead.spam_trap.ip.1h", 3, timedelta(hours=1)),)


def check_create(tenant, *, ip: "str | None", email: str, now) -> Verdict:
    return check(
        tenant, checks=[("ip", ip, CREATE_IP), ("email", email, CREATE_EMAIL)], now=now
    )


def check_spam_trap(tenant, *, ip: "str | None", now) -> Verdict:
    return check(tenant, checks=[("ip", ip, SPAM_TRAP_IP)], now=now)
