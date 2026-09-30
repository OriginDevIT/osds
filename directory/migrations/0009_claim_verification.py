"""Claim.code_hash/code_message and ClaimVerificationGuard (claims PR 3,
domain_email verification -- spec §9.5, §9.6; decisions.md §4.4)."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('audit', '0004_outboundmessage'),
        ('directory', '0008_importbatchlisting'),
        ('tenants', '0002_installsetup_secret'),
    ]

    operations = [
        migrations.AddField(
            model_name='claim',
            name='code_hash',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='claim',
            name='code_message',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='claim_codes', to='audit.outboundmessage'),
        ),
        migrations.CreateModel(
            name='ClaimVerificationGuard',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('wrong_entries', models.PositiveSmallIntegerField(default=0)),
                ('cooldowns', models.PositiveSmallIntegerField(default=0)),
                ('cooldown_until', models.DateTimeField(blank=True, null=True)),
                ('verification_locked_at', models.DateTimeField(blank=True, null=True)),
                ('rejections', models.PositiveSmallIntegerField(default=0)),
                ('claim_blocked_at', models.DateTimeField(blank=True, null=True)),
                ('last_code_sent_at', models.DateTimeField(blank=True, null=True)),
                ('codes_sent_window_start', models.DateTimeField(blank=True, null=True)),
                ('codes_sent_in_window', models.PositiveSmallIntegerField(default=0)),
                ('claimant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='verification_guards', to='directory.directoryuser')),
                ('listing', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='verification_guards', to='directory.listing')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='claim_verification_guards', to='tenants.tenant')),
            ],
            options={
                'db_table': 'claim_verification_guards',
                'constraints': [models.UniqueConstraint(fields=('tenant', 'listing', 'claimant'), name='uniq_guard_listing_claimant')],
            },
        ),
    ]
