# orders/admin.py
from django.contrib import admin
from django.contrib.contenttypes.admin import GenericTabularInline

from .models import (
    FileAttachment,
    ShippingOption,

    FbiApostilleOrder,
    FbiServicePackage,
    FbiPricingSettings,

    MarriagePricingSettings,
    MarriageOrder,

    EmbassyLegalizationOrder,
    TranslationOrder,
    ApostilleOrder,
    I9VerificationOrder,
    QuoteRequest,
    PhoneCallLead,
    Track,
)


# ====== FILES ======
class FileAttachmentInline(GenericTabularInline):
    model = FileAttachment
    extra = 0
    readonly_fields = ('uploaded_at',)


@admin.register(FileAttachment)
class FileAttachmentAdmin(admin.ModelAdmin):
    list_display = ('id', 'content_type', 'object_id', 'file', 'uploaded_at')
    list_filter = ('content_type', 'uploaded_at')
    search_fields = ('content_type__model',)


# ====== SHIPPING ======
@admin.register(ShippingOption)
class ShippingOptionAdmin(admin.ModelAdmin):
    list_display = ("label", "code", "price")


# ====== FBI ======
@admin.register(FbiServicePackage)
class ServicePackageAdmin(admin.ModelAdmin):
    list_display = ("label", "code", "price")


@admin.register(FbiPricingSettings)
class FBIPricingSettingsAdmin(admin.ModelAdmin):
    list_display = ("price_per_certificate",)


@admin.register(FbiApostilleOrder)
class FbiApostilleOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'package', 'count', 'shipping_option', 'total_price',
                    'is_paid', 'tid_created', 'manager_notified', 'zoho_synced', 'created_at')
    list_filter = ('package', 'shipping_option', 'is_paid', 'tid_created', 'manager_notified', 'zoho_synced', 'created_at')
    search_fields = ('name', 'email', 'country_name', 'address')
    readonly_fields = ('track', 'tid_created', 'manager_notified')
    inlines = [FileAttachmentInline]


# ====== MARRIAGE ======
@admin.register(MarriagePricingSettings)
class MarriagePricingSettingsAdmin(admin.ModelAdmin):
    list_display = ('price',)


@admin.register(MarriageOrder)
class MarriageOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'total_price', 'is_paid', 'tid_created', 'manager_notified', 'zoho_synced', 'created_at')
    list_filter = ('is_paid', 'tid_created', 'manager_notified', 'zoho_synced', 'created_at')
    search_fields = ('name', 'email', 'phone', 'address', 'husband_full_name', 'wife_full_name')
    readonly_fields = ('track', 'tid_created', 'manager_notified')
    inlines = [FileAttachmentInline]


# ====== EMBASSY ======
@admin.register(EmbassyLegalizationOrder)
class EmbassyOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'document_type', 'country', 'zoho_synced', 'created_at')
    list_filter = ('zoho_synced', 'created_at')
    search_fields = ('name', 'email', 'phone', 'address', 'document_type')
    inlines = [FileAttachmentInline]


# ====== TRANSLATION ======
@admin.register(TranslationOrder)
class TranslationOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'languages', 'zoho_synced', 'created_at')
    list_filter = ('zoho_synced', 'created_at')
    search_fields = ('name', 'email', 'phone', 'address', 'languages')
    inlines = [FileAttachmentInline]


# ====== APOSTILLE ======
@admin.register(ApostilleOrder)
class ApostilleOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'type', 'country', 'service_type', 'zoho_synced', 'created_at')
    list_filter = ('zoho_synced', 'service_type', 'created_at')
    search_fields = ('name', 'email', 'phone', 'address', 'type', 'country')
    inlines = [FileAttachmentInline]


# ====== I-9 VERIFICATION ======
@admin.register(I9VerificationOrder)
class I9VerificationOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'appointment_date', 'appointment_time', 'zoho_synced', 'created_at')
    list_filter = ('zoho_synced', 'created_at')
    search_fields = ('name', 'email', 'phone', 'address')
    inlines = [FileAttachmentInline]

# ====== QuoteRequest ======
@admin.register(QuoteRequest)
class QuoteRequestOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'appointment_date', 'appointment_time', 'zoho_synced', 'created_at')
    list_filter = ('zoho_synced', 'created_at')
    search_fields = ('name', 'email', 'phone', 'address')


@admin.register(PhoneCallLead)
class PhoneCallLeadAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'whatconverts_lead_id',
        'contact_name',
        'contact_phone',
        'contact_email',
        'detected_service',
        'lead_score',
        'zoho_synced',
        'matched_with_form',
        'created_at',
    )
    list_filter = (
        'zoho_synced',
        'detected_service',
        'matched_with_form',
        'sentiment',
        'created_at',
    )
    search_fields = (
        'whatconverts_lead_id',
        'contact_name',
        'contact_email',
        'contact_phone',
        'contact_company',
        'landing_url',
    )
    readonly_fields = (
        'whatconverts_lead_id',
        'raw_webhook_data',
        'created_at',
        'updated_at',
        'zoho_lead_id',
        'zoho_attribution_id',
        'matched_order_type',
        'matched_order_id',
    )
    fieldsets = (
        ('Contact Information', {
            'fields': ('contact_name', 'contact_email', 'contact_phone', 'contact_company')
        }),
        ('Call Details', {
            'fields': ('call_duration', 'call_recording_url', 'lead_score', 'lead_status')
        }),
        ('Service Detection', {
            'fields': ('detected_service', 'landing_url', 'lead_url', 'zoho_module')
        }),
        ('Attribution', {
            'fields': ('source', 'medium', 'campaign', 'keyword', 'gclid')
        }),
        ('Location', {
            'fields': ('city', 'state', 'zip_code', 'country'),
            'classes': ('collapse',)
        }),
        ('Device Info', {
            'fields': ('device_type', 'device_make', 'operating_system', 'browser'),
            'classes': ('collapse',)
        }),
        ('AI Analysis', {
            'fields': ('lead_summary', 'sentiment', 'intent', 'spotted_keywords'),
            'classes': ('collapse',)
        }),
        ('Sync Status', {
            'fields': ('zoho_synced', 'zoho_lead_id', 'zoho_attribution_id')
        }),
        ('Duplicate Detection', {
            'fields': ('matched_with_form', 'matched_order_type', 'matched_order_id')
        }),
        ('Meta', {
            'fields': ('whatconverts_lead_id', 'whatconverts_created_at', 'created_at', 'updated_at', 'raw_webhook_data'),
            'classes': ('collapse',)
        }),
    )


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = ('tid', 'updated_at', 'created_at')
    search_fields = ('tid',)
