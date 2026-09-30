"""Replace ``smtp.use_tls`` with ``smtp.security`` (decisions.md §4.5).

Plain dict manipulation over the historical ``Tenant``; imports no live code.
A non-empty ``smtp`` block without ``security`` gets ``starttls`` if
``use_tls`` was truthy, else ``none`` (the old sender's behaviour, a missing
key included); ``use_tls`` is always dropped. An empty block (the wizard's
Skip) is left alone. Idempotent: a second run finds nothing to change.

A tenant that had a username and no TLS ends up ``none`` with a username,
which the sender treats as unavailable (no AUTH without TLS). The migration
prints how many, because mail for those tenants stops until an admin
re-saves the settings. No ``tenant.settings_changed`` event is emitted.
"""

from django.db import migrations


def use_tls_to_security(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    cleartext_auth = 0
    for tenant in Tenant.objects.all().iterator():
        block = (tenant.settings or {}).get("smtp")
        if not isinstance(block, dict) or not block:
            continue
        if "security" not in block:
            block["security"] = "starttls" if block.get("use_tls") else "none"
        block.pop("use_tls", None)
        if block["security"] == "none" and (block.get("username") or "").strip():
            cleartext_auth += 1
        tenant.settings["smtp"] = block
        tenant.save(update_fields=["settings"])
    if cleartext_auth:
        print(
            f"\n  {cleartext_auth} tenant(s) had an SMTP username with no TLS: "
            "mail is unavailable for them until an admin picks STARTTLS or TLS."
        )


class Migration(migrations.Migration):
    dependencies = [("tenants", "0002_installsetup_secret")]

    operations = [migrations.RunPython(use_tls_to_security)]
