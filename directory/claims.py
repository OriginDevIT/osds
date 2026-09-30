"""``domain_email`` claim verification mechanics (spec §9.5, §9.6;
decisions.md §4.4).

Kept separate from ``directory.services`` -- a sizeable, self-contained unit,
the way ``directory.masking`` and ``directory.normalize`` already are.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from django.utils.crypto import salted_hmac

from osds.adapters import email_available

# Social, marketplace and free-hosted-site registrable domains (spec §9.6).
# Matched on equality or subdomain, never tenant-configurable. Not
# exhaustive -- a sample the tests pin, extended as gaps are found.
PLATFORM_HOSTS = frozenset(
    {
        "facebook.com",
        "instagram.com",
        "x.com",
        "twitter.com",
        "linkedin.com",
        "yelp.com",
        "youtube.com",
        "tiktok.com",
        "pinterest.com",
        "etsy.com",
        "amazon.com",
        "ebay.com",
        "wixsite.com",
        "squarespace.com",
        "weebly.com",
        "godaddysites.com",
        "business.site",
        "wordpress.com",
        "blogspot.com",
        "carrd.co",
        "linktr.ee",
        "google.com",
        "g.page",
        "square.site",
    }
)

_CODE_SALT = "osds.claim.verification_code"


def normalize_host(host: str, *, strip_www: bool = False) -> str:
    """Lowercase, IDNA-to-ASCII, strip a trailing port and a trailing dot.

    ``strip_www`` additionally strips exactly one leading ``www.`` --
    applied to the website side only (decisions.md §4.4 "Platform hosts":
    "exactly one leading www. stripped from the website host only").
    """
    host = (host or "").strip().lower()
    if not host:
        return host
    host = host.split(":", 1)[0]  # drop a trailing port
    host = host.rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass  # already ASCII, or not a valid hostname -- comparison just won't match
    if strip_www and host.startswith("www."):
        host = host[4:]
    return host


def _website_host(listing) -> "str | None":
    website = (listing.website or "").strip()
    if not website:
        return None
    parsed = urlsplit(website if "//" in website else f"//{website}")
    host = parsed.hostname
    return host or None


def domain_email_host(listing) -> "str | None":
    """The normalized host a ``domain_email`` claim on ``listing`` must
    match, or ``None`` if the method isn't available at all: no website, or
    a website on a platform host (spec §9.6)."""
    host = _website_host(listing)
    if not host:
        return None
    host = normalize_host(host, strip_www=True)
    if host in PLATFORM_HOSTS:
        return None
    labels = host.split(".")
    if any(".".join(labels[i:]) in PLATFORM_HOSTS for i in range(1, len(labels))):
        return None
    return host


def domain_email_eligible(listing, email: str) -> bool:
    """Whether ``email`` may use ``domain_email`` to claim ``listing``
    (spec §9.6, decisions.md §4.4).

    False when there is no website, the website is a platform host, the
    email's domain doesn't match it, or the ``(listing, email)`` pair is
    locked -- "a locked pair is ineligible" folds the lock into eligibility
    itself, since the check needs a DB lookup regardless once a website
    match is confirmed.
    """
    host = domain_email_host(listing)
    if host is None:
        return False
    _, _, domain = (email or "").rpartition("@")
    if not domain or normalize_host(domain) != host:
        return False

    return not _pair_locked(listing, email)


def _pair_locked(listing, email: str) -> bool:
    from directory.models import ClaimVerificationGuard

    guard = ClaimVerificationGuard.objects.filter(
        listing=listing, claimant__email=email
    ).first()
    return bool(guard and guard.verification_locked_at)


def domain_email_ineligibility(tenant, listing, email: str) -> "str | None":
    """Why ``email`` may not use ``domain_email`` on ``listing``, or ``None``
    if it may (decisions.md §4.4, §4.5). In presentation order:

    * ``no_website`` -- no website, or a platform host;
    * ``locked`` -- the (listing, email) pair is locked;
    * ``mail_unavailable`` -- ``email.send`` is not configured for the
      tenant, so no address could ever receive a code. Ranked above the
      address because fixing the address cannot help once mail is down;
    * ``address_mismatch`` -- the address is not at the website's host.

    Submit and ``claim.start_verification`` both flip to ``manual`` on any
    non-``None`` result; only the submitted page tells the reasons apart.
    """
    host = domain_email_host(listing)
    if host is None:
        return "no_website"
    if _pair_locked(listing, email):
        return "locked"
    if not email_available(tenant):
        return "mail_unavailable"
    _, _, domain = (email or "").rpartition("@")
    if not domain or normalize_host(domain) != host:
        return "address_mismatch"
    return None


def generate_code() -> str:
    """A six-digit single-use code (spec §9.5), zero-padded."""
    return f"{secrets.randbelow(10**6):06d}"


def hash_code(claim_public_id: str, code: str) -> str:
    """Keyed hash of ``code``, bound to the claim it was issued for
    (decisions.md §4.4: "keying from OSDS_SECRET_KEY... is heavy to
    rotate" -- rejected in favour of Django's own ``SECRET_KEY``, via
    ``salted_hmac``'s default)."""
    return salted_hmac(
        _CODE_SALT, f"{claim_public_id}:{code}", algorithm="sha256"
    ).hexdigest()
