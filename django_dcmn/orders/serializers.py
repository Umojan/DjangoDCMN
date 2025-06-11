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
        # Все поля, кроме служебных, для записи данных
        fields = [
            'id',
            'name', 'email', 'phone', 'address',
            'husband_full_name', 'wife_full_name',
            'marriage_date', 'country', 'comments',
            'marriage_number',
            'total_price', 'is_paid', 'created_at',
        ]
        read_only_fields = ('total_price', 'is_paid', 'created_at')