STAGE_DEFS = {
    'fbi_apostille': [
        {
            'code': 'document_received',
            'name': 'Document Recieved',
            'desc': 'We have received your documents and are preparing them for the next stage of processing.'
        },
        {
            'code': 'submitted',
            'name': 'Submitted at U.S. DoS',
            'desc': 'Your documents have been submitted to the appropriate U.S. Department of State for authentication.'
        },
        {
            'code': 'processed_dos',
            'name': 'Processed at U.S. DoS',
            'desc': 'The notarized documents have been delivered to the U.S. Department of State for federal authentication. Our liaison is closely monitoring the review and certification process to ensure everything proceeds smoothly.'
        },
        {
            'code': 'translated',
            'name': 'Translated',
            'desc': 'Your documents are currently being translated by our certified translators to meet the requirements of the destination country.'
        },
        {
            'code': 'delivered',
            'name': 'Delivered',
            'desc': 'Your documents have been successfully delivered. Thank you for choosing our services!'
        },
    ],
    'state_apostille': [
        {
            'code': 'document_received',
            'name': 'Document Recieved',
            'desc': 'We have received your documents and are preparing them for the next stage of processing.'
        },
        {
            'code': 'notarized',
            'name': 'Notarized',
            'desc': 'Your documents have been notarized by a certified notary public and are ready for submission.'
        },
        {
            'code': 'submitted',
            'name': 'Submitted to the State Authority',
            'desc': 'Your documents have been submitted to the state authority for apostille certification.'
        },
        {
            'code': 'processed_state',
            'name': 'Processed at State Authority',
            'desc': 'Your documents are being processed by the state authority. We are monitoring the progress to ensure timely completion.'
        },
        {
            'code': 'delivered',
            'name': 'Delivered',
            'desc': 'Your documents have been successfully delivered. Thank you for choosing our services!'
        },
    ],
    'embassy_legalization': [
        {
            'code': 'document_received',
            'name': 'Document Recieved',
            'desc': 'We have received your documents and are preparing them for the embassy legalization process.'
        },
        {
            'code': 'notarized',
            'name': 'Notarized',
            'desc': 'Your documents have been notarized by a certified notary public.'
        },
        {
            'code': 'state_authenticated',
            'name': 'State Authenticated',
            'desc': 'Your documents have been authenticated by the state authority and are ready for federal processing.'
        },
        {
            'code': 'federal_authenticated',
            'name': 'Federal DoS Authenticated',
            'desc': 'Your documents have been authenticated by the U.S. Department of State and are ready for embassy legalization.'
        },
        {
            'code': 'embassy_legalized',
            'name': 'Embassy / Consulate Legalized',
            'desc': 'Your documents have been legalized by the embassy or consulate of the destination country.'
        },
        {
            'code': 'translated',
            'name': 'Translated',
            'desc': 'Your documents are being translated by certified translators to meet the requirements of the destination country.'
        },
        {
            'code': 'delivered',
            'name': 'Delivered',
            'desc': 'Your documents have been successfully delivered. Thank you for choosing our services!'
        },
    ],
    'translation': [
        {
            'code': 'document_received',
            'name': 'Document Received',
            'desc': 'We have received your documents and are preparing them for translation.'
        },
        {
            'code': 'in_translation',
            'name': 'In Translation',
            'desc': 'Your documents are currently being translated by our certified translators.'
        },
        {
            'code': 'translated',
            'name': 'Translated',
            'desc': 'Your documents have been translated and are undergoing quality review.'
        },
        {
            'code': 'quality_approved',
            'name': 'Quality Approved',
            'desc': 'The translation has been reviewed and approved by our quality assurance team. Your documents are ready for delivery.'
        },
        {
            'code': 'delivered',
            'name': 'Delivered',
            'desc': 'Your translated documents have been successfully delivered. Thank you for choosing our services!'
        },
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
        'pending submission': 'document_received',
        'order submission stage ( automation email)': 'document_received',
        'state department submission with drop-off/pick-up slip': 'submitted',
        'pick-up of documents from the state department': 'processed_dos',
        'ups label has been generated (automation email)': 'delivered',
        'resubmissions on company cost': 'submitted',
        'rejected': 'document_received',
        'send review (happy clients) (automation emails)': 'delivered',
        'no review ( unhappy client)': 'delivered',
        'documents dropped off at ups store or client’s address': 'delivered',
        'under translation': 'translated',
        'no label / not yet dropped off': 'submitted',
        'fully refunded ( cancelled orders)': 'delivered',
        'from apostille request': 'document_received',
        'notarization': 'submitted',
        'court': 'submitted',
        'secretary of state': 'processed_dos',
        'usdos': 'processed_dos',
        'translation': 'translated',
        'embassy': 'processed_dos',
        'ups/fedex/dhl drop off': 'delivered',
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
        # canonical stages: document_received → in_translation → translated → quality_approved → delivered
        # zoho field: Translation Status (exact values lowercased)
        'client placed request': 'document_received',
        'translation leads from get quote pipeline': 'document_received',
        'initial contact': 'document_received',
        'call follow up': 'document_received',
        'email 1. follow up': 'document_received',
        'email 2. follow up': 'document_received',
        'in progress ✅ (client agreed to proceed, notarization (if required)': 'in_translation',
        'completed': 'translated',
        'shipping/ drop off': 'quality_approved',
        'completed ✅ (send review)': 'delivered',
        'no review ❌ ( uncertified client)': 'delivered',
        'from fbi translation requets': 'document_received',
        'client lost ❌ (silent or chose another provider)': 'document_received',
        'cancelled': 'document_received',
    },
}


