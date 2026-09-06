"""Full-text search: the search_vector column, pg_trgm, supporting indexes,
and a backfill.

CREATE EXTENSION and CREATE INDEX need the database owner. The container
entrypoint runs migrations as DATABASE_URL_ADMIN; the app runs as
DATABASE_URL (ruling 9).
"""

import django.contrib.postgres.indexes
import django.contrib.postgres.search
import django.db.models.deletion
import django.utils.timezone
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations, models


def backfill_search_vectors(apps, schema_editor):
    """Populate search_vector for existing rows. A no-op on a fresh install
    (no tenants yet); backfills an upgrade. Uses the real recompute function
    so the weighting stays in one place (ruling 19)."""
    from directory.models import Listing
    from directory.search import recompute_search_vector
    from osds.tenancy import tenant_context
    from tenants.models import Tenant

    for tenant in Tenant.objects.all().iterator():
        with tenant_context(tenant):
            for listing in Listing.objects.all().iterator():
                recompute_search_vector(listing)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0002_initial'),
        ('directory', '0003_pathredirect'),
        ('tenants', '0002_installsetup_secret'),
    ]

    operations = [
        migrations.CreateModel(
            name='SearchReindexJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('scope', models.CharField(choices=[('tenant', 'Whole tenant'), ('listing_type', 'Listing type'), ('category', 'Category')], max_length=16)),
                ('scope_ref', models.CharField(blank=True, max_length=40)),
                ('reason', models.CharField(blank=True, max_length=120)),
                ('requested_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('done_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'search_reindex_jobs',
            },
        ),
        migrations.AddField(
            model_name='searchreindexjob',
            name='tenant',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reindex_jobs', to='tenants.tenant'),
        ),
        migrations.AddIndex(
            model_name='searchreindexjob',
            index=models.Index(fields=['done_at', 'id'], name='search_rein_done_at_832d7a_idx'),
        ),
        migrations.AddField(
            model_name='listing',
            name='search_vector',
            field=django.contrib.postgres.search.SearchVectorField(editable=False, null=True),
        ),
        TrigramExtension(),
        migrations.AddIndex(
            model_name='listing',
            index=django.contrib.postgres.indexes.GinIndex(fields=['search_vector'], name='listings_search_gin'),
        ),
        migrations.AddIndex(
            model_name='listing',
            index=django.contrib.postgres.indexes.GinIndex(fields=['name'], name='listings_name_trgm', opclasses=['gin_trgm_ops']),
        ),
        migrations.AddIndex(
            model_name='listing',
            index=models.Index(condition=models.Q(('lat__isnull', False)), fields=['lat', 'lon'], name='listings_lat_lon'),
        ),
        migrations.RunPython(backfill_search_vectors, migrations.RunPython.noop),
    ]
