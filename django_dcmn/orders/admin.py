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
                    'is_paid', 'zoho_synced', 'created_at')
    list_filter = ('package', 'shipping_option', 'is_paid', 'created_at')
    search_fields = ('name', 'email', 'country_name', 'address')
    inlines = [FileAttachmentInline]


# ====== MARRIAGE ======
@admin.register(MarriagePricingSettings)
class MarriagePricingSettingsAdmin(admin.ModelAdmin):
    list_display = ('price',)


@admin.register(MarriageOrder)
class MarriageOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'total_price', 'is_paid', 'created_at')
    list_filter = ('is_paid', 'created_at')
    search_fields = ('name', 'email', 'phone', 'address', 'husband_full_name', 'wife_full_name')
    inlines = [FileAttachmentInline]


# ====== EMBASSY ======
@admin.register(EmbassyLegalizationOrder)
class EmbassyOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'document_type', 'country', 'zoho_synced', 'created_at')
    list_filter = ('zoho_synced', 'created_at')
    search_fields = ('name', 'email', 'phone', 'address', 'document_type')
    inlines = [FileAttachmentInline]
