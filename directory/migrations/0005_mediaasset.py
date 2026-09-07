"""MediaAsset: one uploaded image attached to a listing.

Canonical for asset facts (bytes, dimensions, processing state, storage key,
provenance); Listing.media is a projection rebuilt from the ready rows. See
directory/media.py and decisions.md §4.1.
"""

import django.db.models.deletion
import django.utils.timezone
import osds.ids
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('directory', '0004_search_vector'),
        ('tenants', '0002_installsetup_secret'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MediaAsset',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.CharField(default=osds.ids.media_id, editable=False, max_length=40, unique=True)),
                ('role', models.CharField(choices=[('logo', 'Logo'), ('cover', 'Cover'), ('gallery', 'Gallery')], max_length=10)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('ready', 'Ready'), ('failed', 'Failed'), ('quarantined', 'Quarantined')], default='pending', max_length=12)),
                ('original_filename', models.CharField(blank=True, max_length=255)),
                ('content_type', models.CharField(blank=True, max_length=100)),
                ('byte_size', models.PositiveBigIntegerField(default=0)),
                ('width', models.PositiveIntegerField(blank=True, null=True)),
                ('height', models.PositiveIntegerField(blank=True, null=True)),
                ('checksum_sha256', models.CharField(blank=True, max_length=64)),
                ('storage_key', models.CharField(blank=True, max_length=500)),
                ('derivatives', models.JSONField(blank=True, default=dict)),
                ('alt_text', models.CharField(blank=True, max_length=255)),
                ('sort_order', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('listing', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='media_assets', to='directory.listing')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='media_assets', to='tenants.tenant')),
                ('uploaded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='media_assets_uploaded', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'media_assets',
                'ordering': ['sort_order', 'id'],
                'indexes': [models.Index(fields=['tenant', 'listing', 'role'], name='media_asset_tenant__92a7db_idx')],
            },
        ),
    ]
