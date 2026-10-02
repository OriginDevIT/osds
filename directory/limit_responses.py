"""The public response to a rate-limited command (#210, decisions.md §4.8).

Shared by every public form that enforces ``audit.ratelimit``: the claim views
and the lead form. A refusal is a 429 on the page that was asked for, with
``Retry-After`` and the wait in words. Which rule fired is never shown.
"""

from __future__ import annotations

from audit.ratelimit import RateLimited


def wait_text(seconds: int) -> str:
    """A wait as a human would say it, rounded up: never "0 minutes"."""
    if seconds < 60:
        return "a minute"
    minutes = -(-seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = -(-minutes // 60)
    return f"{hours} hour{'s' if hours != 1 else ''}"


def limited(response, exc: RateLimited):
    """Turn a rendered page into the 429 for ``exc``."""
    response.status_code = 429
    response["Retry-After"] = str(exc.retry_after)
    return response


def limit_message(exc: RateLimited) -> str:
    return f"Too many attempts. Please try again in {wait_text(exc.retry_after)}."
