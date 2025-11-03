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
    """
    Serializer для публичной страницы трекинга.
    Возвращает полный timeline с статусами каждого этапа.
    """
    name = serializers.CharField()
    service = serializers.CharField()
    service_label = serializers.CharField()
    created_at = serializers.CharField()
    last_update = serializers.CharField()
    
    # Timeline с детальной информацией о каждом этапе
    timeline = serializers.ListField()
    
    # Текущий этап
    current_stage = serializers.DictField()
    
    # Комментарий (если есть)
    comment = serializers.CharField(allow_blank=True, allow_null=True)
    
    # Дополнительные данные
    shipping = serializers.CharField(allow_blank=True, allow_null=True)
    translation_required = serializers.BooleanField()

    @staticmethod
    def build_timeline(track):
        """
        Строит timeline для трекинга.
        Возвращает список этапов с их статусами (без описаний).
        Детальное описание только для текущего этапа.
        """
        data = track.data or {}
        service = data.get('service')
        current_stage_code = data.get('current_stage')
        translation_required = data.get('translation_r', False)
        comment = data.get('comment', '')
        
        # Получаем определения этапов для сервиса
        defs = STAGE_DEFS.get(service, [])
        
        # Фильтруем этапы: убираем "translated" если перевод не нужен
        filtered_defs = []
        for d in defs:
            if d['code'] == 'translated' and not translation_required:
                continue
            filtered_defs.append(d)
        
        # Находим индекс текущего этапа
        codes = [d['code'] for d in filtered_defs]
        try:
            current_idx = codes.index(current_stage_code) if current_stage_code in codes else 0
        except (ValueError, AttributeError):
            current_idx = 0
        
        # Строим timeline (только название и статус)
        timeline = []
        for i, stage_def in enumerate(filtered_defs):
            # Определяем статус этапа
            if i < current_idx:
                status = 'completed'  # Пройденный этап (черная галочка)
            elif i == current_idx:
                status = 'current'    # Текущий этап (синий с часиками)
            else:
                status = 'pending'    # Будущий этап (серый)
            
            timeline.append({
                'name': stage_def['name'],
                'status': status,
            })
        
        # Текущий этап с развернутым описанием
        current_stage_info = {
            'name': filtered_defs[current_idx]['name'] if current_idx < len(filtered_defs) else '',
            'description': comment if comment else (filtered_defs[current_idx]['desc'] if current_idx < len(filtered_defs) else ''),
        }
        
        return timeline, current_stage_info

    @classmethod
    def from_track(cls, track):
        data = track.data or {}
        service = data.get('service', '')
        
        timeline, current_stage_info = cls.build_timeline(track)
        
        return cls({
            "name": data.get('name', ''),
            "service": service,
            "service_label": service_label(service) if service else '',
            "created_at": track.created_at.isoformat() if track.created_at else '',
            "last_update": track.updated_at.isoformat() if track.updated_at else '',
            "timeline": timeline,
            "current_stage": current_stage_info,
            "comment": data.get('comment', ''),
            "shipping": data.get('shipping', ''),
            "translation_required": data.get('translation_r', False),
        })