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
    Track,
)

from .constants import STAGE_DEFS
from .utils import service_label


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


# ====== TRACKING ======
class TrackSerializer(serializers.ModelSerializer):
    class Meta:
        model = Track
        fields = ['tid', 'data', 'created_at', 'updated_at']


class PublicTrackSerializer(serializers.Serializer):
    name = serializers.CharField()
    service = serializers.CharField()
    last_update = serializers.CharField()
    steps = serializers.DictField(child=serializers.BooleanField())
    current_step_name = serializers.CharField(allow_blank=True)
    current_step_desc = serializers.CharField(allow_blank=True)
    comment = serializers.CharField(allow_blank=True)

    @staticmethod
    def build_steps(track):
        service = (track.data or {}).get('service')
        current_stage = (track.data or {}).get('current_stage')
        defs = STAGE_DEFS.get(service, [])
        codes = [d['code'] for d in defs]
        try:
            current_idx = codes.index(current_stage) if current_stage in codes else -1
        except ValueError:
            current_idx = -1
        steps = {}
        for i, d in enumerate(defs):
            steps[d['name']] = (i <= current_idx and current_idx >= 0)
        current_name = defs[current_idx]['name'] if 0 <= current_idx < len(defs) else ''
        current_desc = defs[current_idx]['desc'] if 0 <= current_idx < len(defs) else ''
        return steps, current_name, current_desc

    @classmethod
    def from_track(cls, track):
        steps, current_name, current_desc = cls.build_steps(track)
        service = (track.data or {}).get('service')
        return cls({
            "name": (track.data or {}).get('name') or "",
            "service": service_label(service) if service else "",
            "last_update": track.updated_at.isoformat(),
            "steps": steps,
            "current_step_name": current_name,
            "current_step_desc": current_desc,
            "comment": (track.data or {}).get('comment') or "",
        })