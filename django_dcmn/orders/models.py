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

    class Meta:
        verbose_name = '⚙️ Shipping Option'
        verbose_name_plural = '⚙️ Shipping Options'


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
class FileAttachment(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')

    file = models.FileField(upload_to='orders/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Attachment for {self.content_type.model} #{self.object_id}"

    class Meta:
        verbose_name = '⚙️ File Attachment'
        verbose_name_plural = '⚙️ File Attachments'


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
    tid_created = models.BooleanField(default=False, help_text="Tracking ID created")
    manager_notified = models.BooleanField(default=False, help_text="Manager email sent")
    track = models.ForeignKey('Track', on_delete=models.SET_NULL, null=True, blank=True, related_name='fbi_orders')
    attribution_data = models.JSONField(blank=True, null=True, help_text="Marketing attribution data")
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
    zoho_synced = models.BooleanField(default=False)
    tid_created = models.BooleanField(default=False, help_text="Tracking ID created")
    manager_notified = models.BooleanField(default=False, help_text="Manager email sent")
    track = models.ForeignKey('Track', on_delete=models.SET_NULL, null=True, blank=True, related_name='marriage_orders')
    attribution_data = models.JSONField(blank=True, null=True, help_text="Marketing attribution data")

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


class EmbassyLegalizationOrder(models.Model):
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    address = models.TextField()
    document_type = models.CharField(max_length=255)
    country = models.CharField(max_length=100)
    comments = models.TextField(blank=True, null=True)

    zoho_synced = models.BooleanField(default=False)
    attribution_data = models.JSONField(blank=True, null=True, help_text="Marketing attribution data")
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


class TranslationOrder(models.Model):
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    address = models.TextField()
    languages = models.TextField(help_text="Selected languages as comma-separated list")
    comments = models.TextField(blank=True, null=True)

    zoho_synced = models.BooleanField(default=False)
    attribution_data = models.JSONField(blank=True, null=True, help_text="Marketing attribution data")
    created_at = models.DateTimeField(auto_now_add=True)

    file_attachments = GenericRelation(
        FileAttachment,
        content_type_field='content_type',
        object_id_field='object_id',
        related_query_name='translation_order'
    )

    def __str__(self):
        return f"Translation Order #{self.id} by {self.name}"

    class Meta:
        verbose_name = 'Translation — Order'
        verbose_name_plural = 'Translation — Orders'


class ApostilleOrder(models.Model):
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    type = models.CharField(max_length=255, help_text="Type of documents")
    country = models.CharField(max_length=100, help_text="Country of legalization")
    service_type = models.CharField(
        max_length=100,
        help_text="Service location type: Office, UPS, or My Address"
    )
    address = models.TextField(blank=True, null=True, help_text="Client address, if applicable")
    comments = models.TextField(blank=True, null=True)

    zoho_synced = models.BooleanField(default=False)
    attribution_data = models.JSONField(blank=True, null=True, help_text="Marketing attribution data")
    created_at = models.DateTimeField(auto_now_add=True)

    file_attachments = GenericRelation(
        FileAttachment,
        content_type_field='content_type',
        object_id_field='object_id',
        related_query_name='apostille_order'
    )

    def __str__(self):
        return f"Apostille Order #{self.id} by {self.name}"

    class Meta:
        verbose_name = 'Apostille — Order'
        verbose_name_plural = 'Apostille — Orders'


class I9VerificationOrder(models.Model):
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    address = models.TextField()
    appointment_date = models.CharField()
    appointment_time = models.CharField()
    comments = models.TextField(blank=True, null=True)

    zoho_synced = models.BooleanField(default=False)
    attribution_data = models.JSONField(blank=True, null=True, help_text="Marketing attribution data")
    created_at = models.DateTimeField(auto_now_add=True)

    file_attachments = GenericRelation(
        FileAttachment,
        content_type_field='content_type',
        object_id_field='object_id',
        related_query_name='i9_verification_order'
    )

    def __str__(self):
        return f"I-9 Verification Order #{self.id} by {self.name}"

    class Meta:
        verbose_name = "I-9 Verification — Order"
        verbose_name_plural = "I-9 Verification — Orders"


class QuoteRequest(models.Model):
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    address = models.TextField(blank=True, null=True, help_text="Client address, if applicable")
    number = models.CharField(max_length=50, default=1)
    appointment_date = models.CharField(max_length=50)
    appointment_time = models.CharField(max_length=50)
    services = models.TextField(help_text="selected service")
    comments = models.TextField(blank=True, null=True)

    zoho_synced = models.BooleanField(default=False)
    attribution_data = models.JSONField(blank=True, null=True, help_text="Marketing attribution data")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Quote Request #{self.id} by {self.name}"

    class Meta:
        verbose_name = 'Quote — Request'
        verbose_name_plural = 'Quote — Requests'


# --- Phone Call Leads (WhatConverts) ---
class PhoneCallLead(models.Model):
    """Store phone call leads from WhatConverts"""

    # WhatConverts identifiers
    whatconverts_lead_id = models.CharField(max_length=100, unique=True, db_index=True, help_text="WhatConverts lead_id")

    # Contact information
    contact_name = models.CharField(max_length=255, blank=True)
    contact_email = models.EmailField(blank=True, null=True, db_index=True)
    contact_phone = models.CharField(max_length=50, blank=True, db_index=True)
    contact_company = models.CharField(max_length=255, blank=True)

    # Call details
    call_duration = models.IntegerField(null=True, blank=True, help_text="Duration in seconds")
    call_recording_url = models.URLField(max_length=500, blank=True, null=True)
    lead_score = models.IntegerField(null=True, blank=True, help_text="WhatConverts lead score")
    lead_status = models.CharField(max_length=50, blank=True, help_text="WhatConverts lead status")

    # Service detection
    detected_service = models.CharField(max_length=100, blank=True, help_text="Auto-detected service from landing URL")
    landing_url = models.URLField(max_length=500, blank=True)
    lead_url = models.URLField(max_length=500, blank=True, help_text="Page where lead was generated")

    # Attribution data
    source = models.CharField(max_length=100, blank=True)
    medium = models.CharField(max_length=100, blank=True)
    campaign = models.CharField(max_length=255, blank=True)
    keyword = models.CharField(max_length=255, blank=True)
    gclid = models.CharField(max_length=255, blank=True, help_text="Google Click ID")

    # Location
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=50, blank=True)
    zip_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=50, blank=True)

    # Device info
    device_type = models.CharField(max_length=50, blank=True)
    device_make = models.CharField(max_length=100, blank=True)
    operating_system = models.CharField(max_length=100, blank=True)
    browser = models.CharField(max_length=100, blank=True)

    # AI Analysis from WhatConverts
    lead_summary = models.TextField(blank=True, help_text="AI-generated summary")
    sentiment = models.CharField(max_length=50, blank=True, help_text="Positive/Negative/Neutral")
    intent = models.TextField(blank=True, help_text="Detected intent")
    spotted_keywords = models.CharField(max_length=500, blank=True)

    # Full webhook data
    raw_webhook_data = models.JSONField(help_text="Complete WhatConverts webhook payload")

    # Sync status
    zoho_synced = models.BooleanField(default=False)
    zoho_lead_id = models.CharField(max_length=100, blank=True, help_text="Zoho Lead/Deal ID")
    zoho_attribution_id = models.CharField(max_length=100, blank=True, help_text="Zoho Attribution Record ID")
    zoho_module = models.CharField(max_length=100, blank=True, help_text="Target Zoho module (FBI_Apostille, etc)")

    # Duplicate detection
    matched_with_form = models.BooleanField(default=False, help_text="Matched with web form submission")
    matched_order_type = models.CharField(max_length=100, blank=True)
    matched_order_id = models.IntegerField(null=True, blank=True)

    # Timestamps
    whatconverts_created_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        name = self.contact_name or self.contact_phone or f"Lead {self.whatconverts_lead_id}"
        service = self.detected_service or "Unknown"
        return f"{name} | {service} | Phone Call"

    class Meta:
        verbose_name = 'Phone Call Lead'
        verbose_name_plural = 'Phone Call Leads'
        ordering = ['-created_at']


# --- Tracking ---
class Track(models.Model):
    tid = models.CharField(max_length=20, unique=True, db_index=True)
    service = models.CharField(max_length=50, blank=True, null=True, db_index=True)
    # Храним все данные для фронта в одном JSON-объекте
    # Ожидаемые ключи (по желанию): name, email, service, current_stage, comment, shipping, translation_r, и др.
    data = models.JSONField(default=dict, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.tid}"