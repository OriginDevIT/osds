"""Bounds for claim-verification code lifetimes (spec §9.5, decisions.md
§4.4 "§9.5 bounds are a core constant validated at configuration").

Lives here, not under ``directory/``, so ``tenants.services.update_tenant_settings``
can validate the ``claim_verification`` key without ``tenants`` importing
``directory`` -- ``directory`` already imports ``tenants``, never the reverse
(ruling, 2026-09-11, restated at ``directory/services.py``'s
``get_default_consent_text`` docstring).

Bounds are core's, not the tenant's (§9.5): a tenant may tune a lifetime
within them, never past them. Only ``domain_email`` is enforced -- ``manual``
has no code, and ``phone_otp``/``postcard`` ship in a later PR, though the
shape is recorded now so every method's bounds live in one place.
"""

from __future__ import annotations

# directory.models.Claim.Method's values, duplicated as plain strings so
# this module -- and tenants.services.update_tenant_settings, which
# validates enabled_methods against it -- never imports directory (Q2's
# ruling: tenants can't import directory without a cycle). A directory-side
# test pins these against Claim.Method.values directly.
CLAIM_METHODS = frozenset(
    {"manual", "phone_otp", "domain_email", "gbp_oauth", "postcard"}
)

CLAIM_VERIFICATION_BOUNDS = {
    "phone_otp": {
        "default_minutes": 10,
        "min_minutes": 5,
        "max_minutes": 60,
    },
    "domain_email": {
        "default_minutes": 24 * 60,
        "min_minutes": 15,
        "max_minutes": 48 * 60,
    },
    "postcard": {
        "default_minutes": 21 * 24 * 60,
        "min_minutes": 7 * 24 * 60,
        "max_minutes": 45 * 24 * 60,
    },
}
