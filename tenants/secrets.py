"""Encrypted configuration secrets (spec §8.1).

Ciphertext is Fernet. The Fernet key is derived (HKDF-SHA256) from the
``OSDS_SECRET_KEY`` environment variable, which is deliberately separate from
Django's ``SECRET_KEY``: rotating session signing must not force re-encryption
of every stored credential, and vice versa.

Resolution order for a lookup is tenant override, then deployment-level, then
``ConfigurationError``.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

if TYPE_CHECKING:
    from tenants.models import Secret, Tenant


class ConfigurationError(Exception):
    """A required secret, or the key that would decrypt it, is not configured."""


def _fernet() -> Fernet:
    raw = getattr(settings, "OSDS_SECRET_KEY", "") or ""
    if not raw:
        raise ConfigurationError("OSDS_SECRET_KEY is not set")
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"osds.secret.fernet.v1",
    ).derive(raw.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(material))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:  # wrong key, or tampered ciphertext
        raise ConfigurationError("stored secret could not be decrypted") from exc


def get_secret(key: str, *, tenant: "Tenant | None" = None) -> str:
    """Resolve ``key``: tenant override, then deployment, then raise."""
    from tenants.models import Secret

    if tenant is not None:
        row = Secret.objects.filter(
            scope=Secret.Scope.TENANT, tenant=tenant, key=key
        ).first()
        if row is not None:
            return decrypt(row.ciphertext)

    row = Secret.objects.filter(
        scope=Secret.Scope.DEPLOYMENT, tenant__isnull=True, key=key
    ).first()
    if row is not None:
        return decrypt(row.ciphertext)

    raise ConfigurationError(f"no secret configured for {key!r}")


def set_secret(key: str, value: str, *, tenant: "Tenant | None" = None) -> "Secret":
    """Create or replace a secret at deployment scope, or tenant scope when
    ``tenant`` is given."""
    from tenants.models import Secret

    scope = Secret.Scope.TENANT if tenant is not None else Secret.Scope.DEPLOYMENT
    obj, _created = Secret.objects.update_or_create(
        scope=scope,
        tenant=tenant,
        key=key,
        defaults={"ciphertext": encrypt(value)},
    )
    return obj
