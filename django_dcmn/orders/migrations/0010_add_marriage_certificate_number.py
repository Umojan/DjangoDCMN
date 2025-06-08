from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ('orders', '0009_alter_marriageorder_options_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='marriageorder',
            name='marriage_certificate_number',
            field=models.CharField(max_length=100, blank=True, null=True),
        ),
    ]

