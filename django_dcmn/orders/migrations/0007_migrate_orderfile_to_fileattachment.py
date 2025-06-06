from django.db import migrations
from django.contrib.contenttypes.models import ContentType

def migrate_orderfile_to_fileattachment(apps, schema_editor):
    OrderFile = apps.get_model('orders', 'OrderFile')
    FileAttachment = apps.get_model('orders', 'FileAttachment')
    FbiApostilleOrder = apps.get_model('orders', 'FbiApostilleOrder')
    ContentTypeModel = apps.get_model('contenttypes', 'ContentType')

    fbi_ct = ContentTypeModel.objects.get_for_model(FbiApostilleOrder)

    for orderfile in OrderFile.objects.all():
        FileAttachment.objects.create(
            content_type_id=fbi_ct.id,
            object_id=orderfile.order_id,
            file=orderfile.file,
            uploaded_at=orderfile.uploaded_at
        )

def reverse_func(apps, schema_editor):
    # Не реализуем обратную миграцию
    pass

class Migration(migrations.Migration):
    dependencies = [
        ('orders', '0006_alter_fileattachment_options_and_more'),
    ]

    operations = [
        migrations.RunPython(migrate_orderfile_to_fileattachment, reverse_func),
    ]

