from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0032_fingerprintingsubmission_service_type'),
    ]

    operations = [
        migrations.CreateModel(
            name='ZohoSyncJob',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('order_type', models.CharField(max_length=32)),
                ('order_id', models.PositiveBigIntegerField()),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('pending', 'Pending'),
                            ('running', 'Running'),
                            ('synced', 'Synced'),
                            ('failed', 'Failed'),
                            ('suppressed', 'Suppressed'),
                        ],
                        db_index=True,
                        default='pending',
                        max_length=16,
                    ),
                ),
                ('zoho_module', models.CharField(blank=True, max_length=100)),
                ('zoho_record_id', models.CharField(blank=True, max_length=100)),
                ('tracking_id', models.CharField(blank=True, max_length=64)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('last_error', models.TextField(blank=True)),
                ('last_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('synced_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Zoho Sync Job',
                'verbose_name_plural': 'Zoho Sync Jobs',
                'ordering': ('-updated_at',),
            },
        ),
        migrations.AddConstraint(
            model_name='zohosyncjob',
            constraint=models.UniqueConstraint(
                fields=('order_type', 'order_id'),
                name='orders_unique_zoho_sync_job',
            ),
        ),
        migrations.AddIndex(
            model_name='zohosyncjob',
            index=models.Index(
                fields=['status', 'updated_at'],
                name='orders_zoho_status_updated_idx',
            ),
        ),
    ]
