"""The claim-flow rate limits (spec §9.4, #210, decisions.md §4.8).

Core-fixed, like the §9.5 bounds and the §9.6 attempt caps: an operator raising
an anti-abuse ceiling on a form field is not making a security decision
knowingly, so there is no tenant setting for these. Every key is scoped per
tenant by ``audit.ratelimit``.

"Per account" in spec §9.4 means the claimant's email: claimants have no
accounts, and the email is also the recipient of the code, which is the one
thing a per-IP limit cannot protect (decisions.md §4.4, "Sends are capped per
pair").

The constants are read at call time, so a test that needs a different limit
patches the constant in its own module.
"""

from __future__ import annotations

from datetime import timedelta

from audit.ratelimit import Rule, Verdict, check

SUBMIT_IP = (
    Rule("claim.submit.ip.10m", 5, timedelta(minutes=10)),
    Rule("claim.submit.ip.24h", 20, timedelta(hours=24)),
)
SUBMIT_EMAIL = (Rule("claim.submit.email.24h", 5, timedelta(hours=24)),)
VERIFY_IP = (Rule("claim.verify.ip.15m", 30, timedelta(minutes=15)),)
RESEND_IP = (Rule("claim.resend.ip.1h", 10, timedelta(hours=1)),)
RESEND_EMAIL = (Rule("claim.resend.email.24h", 10, timedelta(hours=24)),)


def check_submit(tenant, *, ip: "str | None", email: str, now) -> Verdict:
    return check(
        tenant, checks=[("ip", ip, SUBMIT_IP), ("email", email, SUBMIT_EMAIL)], now=now
    )


def check_verify(tenant, *, ip: "str | None", now) -> Verdict:
    return check(tenant, checks=[("ip", ip, VERIFY_IP)], now=now)


def check_resend(tenant, *, ip: "str | None", email: str, now) -> Verdict:
    return check(
        tenant, checks=[("ip", ip, RESEND_IP), ("email", email, RESEND_EMAIL)], now=now
    )
