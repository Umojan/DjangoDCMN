from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('orders', '0024_fbiapostilleorder_manager_notified_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='ReviewRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(db_index=True, max_length=254)),
                ('name', models.CharField(blank=True, default='', max_length=255)),
                ('phone', models.CharField(blank=True, default='', max_length=50)),
                ('zoho_contact_id', models.CharField(db_index=True, max_length=50)),
                ('zoho_deal_id', models.CharField(blank=True, default='', max_length=50)),
                ('zoho_module', models.CharField(blank=True, default='', help_text='Zoho module name (Deals, Triple_Seal_Apostilles, etc.)', max_length=100)),
                ('tracking_id', models.CharField(blank=True, db_index=True, default='', max_length=20)),
                ('review_type', models.CharField(blank=True, choices=[('google', 'Google Review'), ('trustpilot', 'TrustPilot')], default='', help_text='Determined automatically based on Leads Won', max_length=20)),
                ('leads_won_before', models.IntegerField(default=0, help_text='Leads Won before update')),
                ('leads_won_after', models.IntegerField(default=0, help_text='Leads Won after update')),
                ('is_sent', models.BooleanField(default=False, help_text='Whether review request was sent')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('track', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='review_requests', to='orders.track')),
            ],
            options={
                'verbose_name': '📝 Review Request',
                'verbose_name_plural': '📝 Review Requests',
                'ordering': ['-created_at'],
            },
        ),
    ]
