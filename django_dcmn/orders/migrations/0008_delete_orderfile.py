from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [
        ('orders', '0007_migrate_orderfile_to_fileattachment'),
    ]

    operations = [
        migrations.DeleteModel(
            name='OrderFile',
        ),
    ]

