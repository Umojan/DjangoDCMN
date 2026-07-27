from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0033_zoho_sync_job'),
    ]

    operations = [
        migrations.CreateModel(
            name='QuoteRequestDeduplication',
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
                ('fingerprint', models.CharField(max_length=64, unique=True)),
                ('last_accepted_at', models.DateTimeField(blank=True, null=True)),
                (
                    'last_order',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+',
                        to='orders.quoterequest',
                    ),
                ),
            ],
        ),
    ]
