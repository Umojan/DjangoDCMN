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
    PreCheckSubmission,
    FingerprintingSubmission,
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
        read_only_fields = ('is_paid', 'zoho_synced', 'created_at', 'total_price', 'attribution_data')


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
        read_only_fields = ('total_price', 'is_paid', 'created_at', 'attribution_data')


# ====== EMBASSY ======
class EmbassyLegalizationOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmbassyLegalizationOrder
        fields = '__all__'
        read_only_fields = ('zoho_synced', 'created_at', 'attribution_data')


class TranslationOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = TranslationOrder
        fields = '__all__'
        read_only_fields = ('zoho_synced', 'created_at', 'attribution_data')

# ====== APOSTILLE ======
class ApostilleOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApostilleOrder
        fields = '__all__'
        read_only_fields = ('zoho_synced', 'created_at', 'attribution_data')

# ====== I-9 ======
class I9OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = I9VerificationOrder
        fields = '__all__'
        read_only_fields = ('zoho_synced', 'created_at', 'attribution_data')

# ====== Quote ======
class QuoteRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuoteRequest
        fields = '__all__'
        read_only_fields = ('zoho_synced', 'created_at', 'attribution_data')


# ====== Fingerprinting ======
class FingerprintingSubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = FingerprintingSubmission
        fields = '__all__'
        read_only_fields = ('zoho_synced', 'created_at', 'attribution_data')


# ====== Pre-Check ======
class PreCheckSubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PreCheckSubmission
        fields = '__all__'
        read_only_fields = ('zoho_synced', 'created_at', 'attribution_data')


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
        # И убираем "completed" (она скрытая, используется только для статуса)
        filtered_defs = []
        for d in defs:
            if d['code'] == 'translated' and not translation_required:
                continue
            if d['code'] == 'completed':
                continue
            filtered_defs.append(d)
        
        # Находим индекс текущего этапа
        codes = [d['code'] for d in filtered_defs]
        
        if current_stage_code == 'completed':
            # Если стадия completed, то все визуальные этапы пройдены
            current_idx = len(filtered_defs)
        else:
            try:
                current_idx = codes.index(current_stage_code) if current_stage_code in codes else 0
            except (ValueError, AttributeError):
                current_idx = 0
        
        # Специальная логика для document_received:
        # Если текущая стадия = document_received (первая), то показываем её как completed,
        # а следующую как current
        display_mode = 'normal'
        display_idx = current_idx
        
        if current_stage_code != 'completed' and current_idx == 0 and len(filtered_defs) > 0 and filtered_defs[0]['code'] == 'document_received':
            display_mode = 'first_stage_special'
            # Для пайплайнов с quote_review показываем описание второй стадии
            # Для FBI (без quote_review) показываем описание первой стадии
            if len(filtered_defs) > 1 and filtered_defs[1]['code'] == 'quote_review':
                display_idx = 1  # Описание от quote_review (Translation, Apostille, Embassy)
            else:
                display_idx = 0  # Описание от document_received (FBI)
        
        # Строим timeline (только название и статус)
        timeline = []
        for i, stage_def in enumerate(filtered_defs):
            # Определяем статус этапа
            if display_mode == 'first_stage_special':
                # Специальный режим: первая стадия = completed, вторая = current
                if i == 0:
                    status = 'completed'  # Document Received с галочкой
                elif i == 1:
                    status = 'current'    # Следующая стадия как "в процессе"
                else:
                    status = 'pending'    # Остальные = pending
            else:
                # Обычный режим
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
        if current_stage_code == 'completed':
            # Находим определение completed стадии в исходном списке
            completed_def = next((d for d in defs if d['code'] == 'completed'), None)
            stage_name = completed_def['name'] if completed_def else 'Order Completed'
            stage_desc = comment if comment else (completed_def['desc'] if completed_def else '')
        else:
            stage_name = filtered_defs[display_idx]['name'] if display_idx < len(filtered_defs) else ''
            stage_desc = comment if comment else (filtered_defs[display_idx]['desc'] if display_idx < len(filtered_defs) else '')

        current_stage_info = {
            'name': stage_name,
            'description': stage_desc,
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

# ====== Business Account / Partner applications ======
class _FlexibleBooleanField(serializers.BooleanField):
    """Accept true/'true'/'on'/1 from the site bridge; anything else is False."""

    def to_internal_value(self, data):
        if isinstance(data, str) and data.strip().lower() in ('on', 'yes', 'checked'):
            return True
        try:
            return super().to_internal_value(data)
        except serializers.ValidationError:
            return False


class ApplicationSerializer(serializers.Serializer):
    """
    Validates the JSON contract from the Webflow bridge (dcmn-apply.js).

    Select values are validated as free strings (the client may edit the
    selects in Webflow). Unknown keys are ignored. Program-specific keys
    (organization/company, city_state/state_country, ...) are normalised
    onto the shared model columns in `to_application_fields()`.
    """

    MAX_TEXT = 500
    MAX_NOTES = 5000

    program = serializers.CharField(required=False, allow_blank=True)
    contact_name = serializers.CharField(max_length=255, error_messages={
        'required': 'Please enter your name.', 'blank': 'Please enter your name.'})
    email = serializers.EmailField(max_length=254, error_messages={
        'required': 'Please enter your email address.', 'blank': 'Please enter your email address.',
        'invalid': 'Please enter a valid email address.'})
    phone = serializers.CharField(max_length=50, error_messages={
        'required': 'Please enter your phone number.', 'blank': 'Please enter your phone number.'})

    # Business account naming
    organization = serializers.CharField(max_length=255, required=False, allow_blank=True)
    role = serializers.CharField(max_length=MAX_TEXT, required=False, allow_blank=True)
    city_state = serializers.CharField(max_length=MAX_TEXT, required=False, allow_blank=True)
    org_type = serializers.CharField(max_length=MAX_TEXT, required=False, allow_blank=True)
    order_frequency = serializers.CharField(max_length=MAX_TEXT, required=False, allow_blank=True)

    # Partner naming
    company = serializers.CharField(max_length=255, required=False, allow_blank=True)
    website = serializers.CharField(max_length=MAX_TEXT, required=False, allow_blank=True)
    state_country = serializers.CharField(max_length=MAX_TEXT, required=False, allow_blank=True)
    business_type = serializers.CharField(max_length=MAX_TEXT, required=False, allow_blank=True)
    monthly_volume = serializers.CharField(max_length=MAX_TEXT, required=False, allow_blank=True)
    start_timing = serializers.CharField(max_length=MAX_TEXT, required=False, allow_blank=True)
    delivery_preference = serializers.CharField(max_length=MAX_TEXT, required=False, allow_blank=True)
    partner_terms_ack = _FlexibleBooleanField(required=False, default=False)

    # Shared
    services = serializers.ListField(
        child=serializers.CharField(max_length=100), required=False, default=list,
        error_messages={'not_a_list': 'Services must be a list.'})
    countries = serializers.CharField(max_length=MAX_TEXT, required=False, allow_blank=True)
    notes = serializers.CharField(max_length=MAX_NOTES, required=False, allow_blank=True, error_messages={
        'max_length': 'Notes are too long (5000 characters max).'})
    source_page = serializers.CharField(max_length=MAX_TEXT, required=False, allow_blank=True)
    page_url = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    attribution = serializers.JSONField(required=False, allow_null=True)
    turnstile_token = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def __init__(self, *args, program: str, **kwargs):
        self.program = program
        super().__init__(*args, **kwargs)

    def validate_services(self, value):
        return [str(v).strip() for v in value if str(v).strip()][:50]

    def validate_page_url(self, value):
        value = (value or '').strip()
        if value and not value.lower().startswith(('http://', 'https://')):
            return ''
        return value

    def validate(self, attrs):
        from .models import Application
        from rest_framework.exceptions import ValidationError

        if self.program == Application.PROGRAM_PARTNER:
            if not (attrs.get('company') or '').strip():
                raise ValidationError({'detail': 'Please enter your company name.'})
            if not attrs.get('partner_terms_ack'):
                raise ValidationError({'detail': 'Please confirm the partner terms to continue.'})
        else:
            if not (attrs.get('organization') or '').strip():
                raise ValidationError({'detail': 'Please enter your organization name.'})
        return attrs

    def to_application_fields(self) -> dict:
        """Map validated payload onto Application model columns."""
        from .models import Application

        d = self.validated_data
        partner = self.program == Application.PROGRAM_PARTNER
        return {
            'program': self.program,
            'contact_name': d['contact_name'].strip(),
            'email': d['email'].strip().lower(),
            'phone': d['phone'].strip(),
            'organization': (d.get('company') if partner else d.get('organization') or '').strip(),
            'role': (d.get('role') or '').strip(),
            'website': (d.get('website') or '').strip(),
            'location': ((d.get('state_country') if partner else d.get('city_state')) or '').strip(),
            'org_type': ((d.get('business_type') if partner else d.get('org_type')) or '').strip(),
            'services': d.get('services') or [],
            'volume': ((d.get('monthly_volume') if partner else d.get('order_frequency')) or '').strip(),
            'start_timing': (d.get('start_timing') or '').strip(),
            'countries': (d.get('countries') or '').strip(),
            'delivery_preference': (d.get('delivery_preference') or '').strip(),
            'notes': (d.get('notes') or '').strip(),
            'partner_terms_ack': bool(d.get('partner_terms_ack')),
            'source_page': (d.get('source_page') or '').strip(),
            'page_url': d.get('page_url') or '',
        }

    @staticmethod
    def first_error_message(errors) -> str:
        """Flatten DRF errors into one short human-readable sentence."""
        if isinstance(errors, dict):
            if 'detail' in errors:
                return ApplicationSerializer.first_error_message(errors['detail'])
            for key, value in errors.items():
                msg = ApplicationSerializer.first_error_message(value)
                if msg:
                    return msg
            return 'Please check the form and try again.'
        if isinstance(errors, (list, tuple)):
            for item in errors:
                msg = ApplicationSerializer.first_error_message(item)
                if msg:
                    return msg
            return ''
        return str(errors)
