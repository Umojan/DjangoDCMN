# orders/serializers.py
from rest_framework import serializers
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
)


# ====== FILES ======
class FileAttachmentSerializer(serializers.ModelSerializer):
    content_type = serializers.StringRelatedField()  # покажет название модели
    object_id    = serializers.IntegerField()

    class Meta:
        model = FileAttachment
        fields = ['id', 'content_type', 'object_id', 'file', 'uploaded_at']



# ====== SHIPPING ======
class ShippingOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShippingOption
        fields = '__all__'


# ====== FBI ======
class FbiServicePackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = FbiServicePackage
        fields = '__all__'

class FbiApostilleOrderSerializer(serializers.ModelSerializer):
    package = serializers.SlugRelatedField(
        slug_field='code',
        queryset=FbiServicePackage.objects.all()
    )
    shipping_option = serializers.SlugRelatedField(
        slug_field='code',
        queryset=ShippingOption.objects.all()
    )

    class Meta:
        model = FbiApostilleOrder
        fields = '__all__'
        read_only_fields = ('is_paid', 'zoho_synced', 'created_at', 'total_price')


# ====== MARRIAGE ======
class MarriagePricingSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarriagePricingSettings
        fields = ['id', 'price']


class MarriageOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarriageOrder
        fields = [
            'id',
            'name', 'email', 'phone', 'address',
            'husband_full_name', 'wife_full_name',
            'marriage_date', 'country', 'comments',
            'marriage_number',
            'total_price', 'is_paid', 'created_at',
        ]
        read_only_fields = ('total_price', 'is_paid', 'created_at')


# ====== EMBASSY ======
class EmbassyLegalizationOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmbassyLegalizationOrder
        fields = '__all__'
        read_only_fields = ('zoho_synced', 'created_at')


class TranslationOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = TranslationOrder
        fields = '__all__'
        read_only_fields = ('zoho_synced', 'created_at')

# ====== APOSTILLE ======
class ApostilleOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApostilleOrder
        fields = '__all__'
        read_only_fields = ('zoho_synced', 'created_at')

# ====== I-9 ======
class I9OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = I9VerificationOrder
        fields = '__all__'
        read_only_fields = ('zoho_synced', 'created_at',)

# ====== Order ======
class QuoteRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuoteRequest
        fields = '__all__'
        read_only_fields = ('zoho_synced', 'created_at',)