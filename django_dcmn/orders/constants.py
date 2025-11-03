STAGE_DEFS = {
    'fbi_apostille': [
        {'code': 'document_received', 'name': 'Document Received', 'desc': 'We received your documents.'},
        {'code': 'notarized', 'name': 'Notarized', 'desc': 'Documents notarized.'},
        {'code': 'submitted', 'name': 'Submitted', 'desc': 'Documents submitted to processing.'},
        {'code': 'processed_dos', 'name': 'Processed at U.S. DoS', 'desc': 'Processing at U.S. Department of State.'},
        {'code': 'translated', 'name': 'Translated (Optional)', 'desc': 'Documents translated if required.'},
        {'code': 'delivered', 'name': 'Delivered', 'desc': 'Order delivered to you.'},
    ],
    'state_apostille': [
        {'code': 'document_received', 'name': 'Document Received', 'desc': 'We received your documents.'},
        {'code': 'notarized', 'name': 'Notarized', 'desc': 'Documents notarized.'},
        {'code': 'submitted', 'name': 'Submitted', 'desc': 'Documents submitted to state authority.'},
        {'code': 'processed_state', 'name': 'Processed at State Authority', 'desc': 'Processing at state authority.'},
        {'code': 'delivered', 'name': 'Delivered', 'desc': 'Order delivered to you.'},
    ],
    'embassy_legalization': [
        {'code': 'document_received', 'name': 'Document Received', 'desc': 'We received your documents.'},
        {'code': 'notarized', 'name': 'Notarized', 'desc': 'Documents notarized.'},
        {'code': 'state_authenticated', 'name': 'State Authenticated', 'desc': 'State authentication completed.'},
        {'code': 'federal_authenticated', 'name': 'Federal DoS Authenticated', 'desc': 'Federal DoS authentication completed.'},
        {'code': 'embassy_legalized', 'name': 'Embassy / Consulate Legalized', 'desc': 'Legalized by embassy/consulate.'},
        {'code': 'translated', 'name': 'Translated (Optional)', 'desc': 'Documents translated if required.'},
        {'code': 'delivered', 'name': 'Delivered', 'desc': 'Order delivered to you.'},
    ],
    'translation': [
        {'code': 'document_received', 'name': 'Document Received', 'desc': 'We received your documents.'},
        {'code': 'translated', 'name': 'Translated', 'desc': 'Translation completed.'},
        {'code': 'quality_approved', 'name': 'Quality Approved', 'desc': 'Quality check approved.'},
        {'code': 'delivered', 'name': 'Delivered', 'desc': 'Order delivered to you.'},
    ],
}

SERVICE_LABELS = {
    'fbi_apostille': 'FBI Apostille',
    'state_apostille': 'State Apostille',
    'embassy_legalization': 'Embassy Legalization',
    'translation': 'Translation',
}

# Маппинг названий модулей из Zoho webhook → API имена модулей для Zoho API
ZOHO_MODULE_MAP = {
    'FBI_Background_Checks': 'Deals',
    'Embassy_Legalization': 'Embassy_Legalization',
    'Translation_Services': 'Translation_Services',
    'Apostille_Services': 'Apostille_Services',
    'Triple_Seal_Apostilles': 'Triple_Seal_Apostilles',
    'I_9_Verification': 'I_9_Verification',
    'Get_A_Quote_Leads': 'Get_A_Quote_Leads',
}

# Маппинг названий стадий из Zoho → канонические коды
# Ключи нормализуются в нижний регистр для устойчивости к регистру
CRM_STAGE_MAP = {
    'fbi_apostille': {
        # canonical
        # 'order received': 'document_received',
        # 'document received': 'document_received',
        # 'notarized': 'notarized',
        # 'submitted': 'submitted',
        # 'processed at u.s. dos': 'processed_dos',
        # 'translated': 'translated',
        # 'delivered': 'delivered',
        # zoho picklist (exact values lowercased)
        'pending submission': 'submitted',
        'order submission stage ( automation email)': 'submitted',
        'state department submission with drop-off/pick-up slip': 'submitted',
        'pick-up of documents from the state department': 'processed_dos',
        'ups label has been generated (automation email)': 'processed_dos',
        'resubmissions on company cost': 'submitted',
        'rejected': 'document_received',
        'send review (happy clients) (automation emails)': 'delivered',
        'no review ( unhappy client)': 'delivered',
        'documents dropped off at ups store or client’s address': 'processed_dos',
        "documents dropped off at ups store or client's address": 'processed_dos',
        'under translation': 'translated',
        'no label / not yet dropped off': 'submitted',
        'fully refunded ( cancelled orders)': 'delivered',
        'from apostille request': 'document_received',
        'notarization': 'notarized',
        'court': 'notarized',
        'secretary of state': 'processed_dos',
        'usdos': 'processed_dos',
        'translation': 'translated',
        'embassy': 'processed_dos',
        'ups/fedex/dhl drop off': 'processed_dos',
        'delivery and reviews': 'delivered',
    },
    'state_apostille': {
        'order received': 'document_received',
        'document received': 'document_received',
        'notarized': 'notarized',
        'submitted': 'submitted',
        'processed at state authority': 'processed_state',
        'delivered': 'delivered',
    },
    'embassy_legalization': {
        'order received': 'document_received',
        'document received': 'document_received',
        'notarized': 'notarized',
        'state authenticated': 'state_authenticated',
        'federal dos authenticated': 'federal_authenticated',
        'embassy / consulate legalized': 'embassy_legalized',
        'translated': 'translated',
        'delivered': 'delivered',
    },
    'translation': {
        'client placed request': 'document_received',
        'document received': 'document_received',
        'translated': 'translated',
        'quality approved': 'quality_approved',
        'delivered': 'delivered',
    },
}


