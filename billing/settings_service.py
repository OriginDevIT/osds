"""The generic adapter-settings write (decisions.md §4.11).

Core renders an adapter's settings page from the provider's own field list
(``SettingField``), so core never names a vendor. Non-secret fields are
configuration at ``tenant.settings["adapters"][adapter_id]``, written through
``update_tenant_settings`` so the change emits ``tenant.settings_changed``;
secret fields go to the secret store under ``"<adapter_id>_<key>"`` and are
never returned. A blank secret keeps what is stored; an explicit clear removes
it.
"""

from __future__ import annotations

from django.db import transaction

from osds.adapter_context import adapter_config, secret_name
from osds.setting_pattern import pattern_error
from tenants import services as tenant_services
from tenants.secrets import delete_secret, has_secret, set_secret


class AdapterSettingsError(ValueError):
    """A value the settings refuse. Nothing is written."""


def stored_secrets(tenant, provider) -> "set[str]":
    return {
        f.key
        for f in provider.settings_fields()
        if f.secret and has_secret(secret_name(provider.adapter_id, f.key), tenant=tenant)
    }


@transaction.atomic
def update_adapter_settings(tenant, *, provider, values: dict, clear: "set[str]" = frozenset(),
                            changed_by) -> None:
    fields = list(provider.settings_fields())
    known = {f.key for f in fields}
    stray = (set(values) | set(clear)) - known
    if stray:
        raise AdapterSettingsError(f"Unknown setting(s): {', '.join(sorted(stray))}")

    config = adapter_config(tenant, provider.adapter_id)
    errors = []
    new_secrets: dict = {}
    drop: list = []
    for f in fields:
        raw = (values.get(f.key) or "").strip() if isinstance(values.get(f.key), str) else values.get(f.key)
        if f.secret:
            name = secret_name(provider.adapter_id, f.key)
            if f.key in clear:
                drop.append(name)
                if f.required:
                    errors.append(f"{f.label} is required.")
            elif raw:
                new_secrets[name] = raw
            elif f.required and not has_secret(name, tenant=tenant):
                errors.append(f"{f.label} is required.")
        else:
            if raw in (None, ""):
                config.pop(f.key, None)
                if f.required:
                    errors.append(f"{f.label} is required.")
            else:
                config[f.key] = raw
        # A typed value must have the shape the adapter declares.
        typed = new_secrets.get(secret_name(provider.adapter_id, f.key)) if f.secret else config.get(f.key)
        bad = pattern_error(f, typed)
        if bad:
            errors.append(bad)
    if errors:
        raise AdapterSettingsError(" ".join(errors))

    adapters = dict((tenant.settings or {}).get("adapters") or {})
    adapters[provider.adapter_id] = config
    tenant_services.update_tenant_settings(
        tenant=tenant, changes={"adapters": adapters}, changed_by=changed_by
    )
    for name, value in new_secrets.items():
        set_secret(name, value, tenant=tenant)
    for name in drop:
        delete_secret(name, tenant=tenant)
