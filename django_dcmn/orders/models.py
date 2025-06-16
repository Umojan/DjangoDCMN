# orders/models.py
from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.fields import GenericRelation
from django.contrib.contenttypes.models import ContentType


class ShippingOption(models.Model):
    code = models.CharField(max_length=50, unique=True)
    label = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=7, decimal_places=2)

    def __str__(self):
        return self.label


# FBI Model
class FbiServicePackage(models.Model):
    code = models.CharField(max_length=50, unique=True)
    label = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=7, decimal_places=2)

    def __str__(self):
        return self.label

    class Meta:
        verbose_name = 'FBI Apostille — Service Package'
        verbose_name_plural = 'FBI Apostille — Service Packages'


# ---------- Files ------------
# Universal model for attachments
class FileAttachment(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')

    file = models.FileField(upload_to='orders/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Attachment for {self.content_type.model} #{self.object_id}"

    class Meta:
        verbose_name = 'File Attachment'
        verbose_name_plural = 'File Attachments'


# FBI Model
class FbiApostilleOrder(models.Model):
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    country_name = models.CharField(max_length=100)
    address = models.TextField()
    comments = models.TextField(blank=True, null=True)

    package = models.ForeignKey(FbiServicePackage, on_delete=models.CASCADE)
    count = models.PositiveIntegerField(help_text="Price per apostille certificate")
    shipping_option = models.ForeignKey(ShippingOption, on_delete=models.CASCADE)

    total_price = models.DecimalField(max_digits=10, decimal_places=2, help_text="Total Price")

    is_paid = models.BooleanField(default=False)
    zoho_synced = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    file_attachments = GenericRelation(
        FileAttachment,
        content_type_field='content_type',
        object_id_field='object_id',
        related_query_name='fbi_order'
    )

    def __str__(self):
        return f"FBI Apostille Order #{self.id} by {self.name}"

    class Meta:
        verbose_name = 'FBI Apostille — Order'
        verbose_name_plural = 'FBI Apostille — Orders'


class FbiPricingSettings(models.Model):
    price_per_certificate = models.DecimalField(max_digits=6, decimal_places=2, default=25.00)

    def __str__(self):
        return f"Settings (Per Certificate: {self.price_per_certificate})"

    class Meta:
        verbose_name = 'FBI Apostille — Pricing Setting'


# Tripe Seal Marriage
class MarriageOrder(models.Model):
    # Step 1
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    address = models.TextField()

    # Step Upload/Manual
    husband_full_name = models.CharField(max_length=255, blank=True, null=True)
    wife_full_name = models.CharField(max_length=255, blank=True, null=True)
    marriage_date = models.CharField(max_length=255, blank=True, null=True)
    country = models.CharField(max_length=255, blank=True, null=True)
    comments = models.TextField(blank=True, null=True)
    marriage_number = models.CharField(max_length=100, blank=True, null=True)

    total_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.00,
        help_text="Calculated total price for this marriage order"
    )
    is_paid = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    file_attachments = GenericRelation(
        FileAttachment,
        content_type_field='content_type',
        object_id_field='object_id',
        related_query_name='marriage_order'
    )

    def __str__(self):
        return f"MarriageOrder #{self.id} by {self.name}"

    class Meta:
        verbose_name = "Triple Seal Marriage — Order"
        verbose_name_plural = "Triple Seal Marriage — Orders"


class MarriagePricingSettings(models.Model):
    price = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=100.00,
        help_text="Base price (USD) for one Triple Seal Marriage order"
    )

    def __str__(self):
        return f"Marriage Pricing Settings (Price: {self.price})"

    class Meta:
        verbose_name = "Triple Seal Marriage — Pricing Setting"
        verbose_name_plural = "Triple Seal Marriage — Pricing Settings"


# Embassy Legalization Order
class EmbassyLegalizationOrder(models.Model):
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    address = models.TextField()
    document_type = models.CharField(max_length=255)
    country = models.CharField(max_length=100)
    comments = models.TextField(blank=True, null=True)

    zoho_synced = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    file_attachments = GenericRelation(
        FileAttachment,
        content_type_field='content_type',
        object_id_field='object_id',
        related_query_name='embassy_order'
    )

    def __str__(self):
        return f"Embassy Legalization Order #{self.id} by {self.name}"

    class Meta:
        verbose_name = 'Embassy Legalization — Order'
        verbose_name_plural = 'Embassy Legalization — Orders'
