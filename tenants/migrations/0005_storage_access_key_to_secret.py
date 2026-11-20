"""Move the cleartext ``settings["storage"]["access_key"]`` into the encrypted
tenant secret ``storage_access_key`` (#224, storage half). Skipped per row when
``OSDS_SECRET_KEY`` is unset or a secret already exists; the resolver still
reads the legacy field, so a skipped row keeps working. Not reversible: the
cleartext is not restored."""

from django.db import migrations


def forwards(apps, schema_editor):
    from tenants.secrets import ConfigurationError, encrypt

    Tenant = apps.get_model("tenants", "Tenant")
    Secret = apps.get_model("tenants", "Secret")
    for tenant in Tenant.objects.all():
        block = (tenant.settings or {}).get("storage") or {}
        legacy = block.get("access_key")
        if not legacy:
            continue
        if not Secret.objects.filter(scope="tenant", tenant=tenant, key="storage_access_key").exists():
            try:
                ciphertext = encrypt(legacy)
            except ConfigurationError:
                continue
            Secret.objects.create(
                scope="tenant", tenant=tenant, key="storage_access_key", ciphertext=ciphertext,
            )
        block = {k: v for k, v in block.items() if k != "access_key"}
        tenant.settings = {**tenant.settings, "storage": block}
        tenant.save(update_fields=["settings"])


class Migration(migrations.Migration):

    dependencies = [("tenants", "0004_operator_invite_links")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
