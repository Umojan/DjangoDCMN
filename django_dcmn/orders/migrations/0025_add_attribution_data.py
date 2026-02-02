# orders/migrations/0025_add_attribution_data.py
"""Add attribution_data JSONField to all order models for marketing tracking."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('orders', '0024_fbiapostilleorder_manager_notified_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='fbiapostilleorder',
            name='attribution_data',
            field=models.JSONField(
                blank=True,
                null=True,
                help_text='Marketing attribution data'
            ),
        ),
        migrations.AddField(
            model_name='marriageorder',
            name='attribution_data',
            field=models.JSONField(
                blank=True,
                null=True,
                help_text='Marketing attribution data'
            ),
        ),
        migrations.AddField(
            model_name='embassylegalizationorder',
            name='attribution_data',
            field=models.JSONField(
                blank=True,
                null=True,
                help_text='Marketing attribution data'
            ),
        ),
        migrations.AddField(
            model_name='translationorder',
            name='attribution_data',
            field=models.JSONField(
                blank=True,
                null=True,
                help_text='Marketing attribution data'
            ),
        ),
        migrations.AddField(
            model_name='apostilleorder',
            name='attribution_data',
            field=models.JSONField(
                blank=True,
                null=True,
                help_text='Marketing attribution data'
            ),
        ),
        migrations.AddField(
            model_name='i9verificationorder',
            name='attribution_data',
            field=models.JSONField(
                blank=True,
                null=True,
                help_text='Marketing attribution data'
            ),
        ),
        migrations.AddField(
            model_name='quoterequest',
            name='attribution_data',
            field=models.JSONField(
                blank=True,
                null=True,
                help_text='Marketing attribution data'
            ),
        ),
    ]
