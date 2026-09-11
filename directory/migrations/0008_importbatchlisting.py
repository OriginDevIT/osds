"""ImportBatchListing: per-row import provenance and the rollback pre-image.

One row per listing a batch created or updated. ``pre_image`` is the full §4.1
projection captured at update time; rollback restores updated rows from it and
deletes created rows. Nulled at 90 days (spec §3.3, §11.2). See
directory/services.py (capture + rollback_import_batch) and
directory/importing.py (null_import_pre_images).
"""

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('directory', '0007_importbatch_notes'),
        ('tenants', '0002_installsetup_secret'),
    ]

    operations = [
        migrations.CreateModel(
            name='ImportBatchListing',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(choices=[('created', 'Created'), ('updated', 'Updated')], max_length=8)),
                ('pre_image', models.JSONField(blank=True, null=True)),
                ('pre_image_nulled_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('batch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='row_provenance', to='directory.importbatch')),
                ('listing', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='import_provenance', to='directory.listing')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='import_batch_listings', to='tenants.tenant')),
            ],
            options={
                'db_table': 'import_batch_listings',
                'indexes': [models.Index(fields=['tenant', 'batch'], name='import_batc_tenant__7108e4_idx')],
                'constraints': [models.UniqueConstraint(fields=('batch', 'listing'), name='uniq_importbatchlisting_batch_listing')],
            },
        ),
    ]
