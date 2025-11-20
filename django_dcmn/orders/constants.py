STAGE_DEFS = {
    'fbi_apostille': [
        {
            'code': 'document_received',
            'name': 'Document Received',
            'desc': 'We have received your documents and are preparing them for the next stage of processing.'
        },
        {
            'code': 'submitted',
            'name': 'Submission at U.S. DoS',
            'desc': 'Your documents are under Review for Federal authentication.'
        },
        {
            'code': 'processed_dos',
            'name': 'Processing at U.S. DoS',
            'desc': 'Your documents are currently being processed by the U.S. Department of State. Our liaison is closely monitoring the review and certification process to ensure everything proceeds smoothly.'
        },
        {
            'code': 'translated',
            'name': 'Translation',
            'desc': 'Your documents are being translated by our certified translators to meet the requirements of the destination country.'
        },
        {
            'code': 'delivered',
            'name': 'Delivery',
            'desc': 'Your documents are being delivered to you. Thank you for choosing our services!'
        },
        {
            'code': 'completed',
            'name': 'Order Completed',
            'desc': 'Your order has been successfully completed. Thank you!'
        },
    ],
    'state_apostille': [
        {
            'code': 'document_received',
            'name': 'Document Received',
            'desc': 'We have received your documents and are preparing them for the next stage of processing.'
        },
        {
            'code': 'notarized',
            'name': 'Notarization (if required)',
            'desc': 'Your documents are being notarized by a certified notary public to prepare for state authentication.'
        },
        {
            'code': 'submitted',
            'name': 'Submission to State Authority',
            'desc': 'Your documents are being submitted to the state authority for apostille certification.'
        },
        {
            'code': 'processed_state',
            'name': 'Processing at State Authority',
            'desc': 'Your documents are being processed by the state authority. We are monitoring the progress to ensure timely completion.'
        },
        {
            'code': 'delivered',
            'name': 'Delivery',
            'desc': 'Your documents are being delivered to you. Thank you for choosing our services!'
        },
    ],
    'embassy_legalization': [
        {
            'code': 'document_received',
            'name': 'Document Received',
            'desc': 'We have received your documents and are preparing them for the embassy legalization process.'
        },
        {
            'code': 'notarized',
            'name': 'Notarization (if required)',
            'desc': 'Your documents are being notarized by a certified notary public.'
        },
        {
            'code': 'state_authenticated',
            'name': 'State Authentication',
            'desc': 'Your documents are being authenticated by the state authority to prepare for federal processing.'
        },
        {
            'code': 'federal_authenticated',
            'name': 'Federal DoS Authentication',
            'desc': 'Your documents are being authenticated by the U.S. Department of State to prepare for embassy legalization.'
        },
        {
            'code': 'embassy_legalized',
            'name': 'Embassy / Consulate Legalization',
            'desc': 'Your documents are being legalized by the embassy or consulate of the destination country.'
        },
        {
            'code': 'delivered',
            'name': 'Delivery',
            'desc': 'Your documents are being delivered to you.'
        },
        {
            'code': 'completed',
            'name': 'Order Completed',
            'desc': 'Your order has been successfully completed. Thank you for choosing our services!'
        },
    ],
    'translation': [
        {
            'code': 'document_received',
            'name': 'Document Received',
            'desc': 'We have received your documents and will review them shortly. Our team will be in touch with you soon.'
        },
        {
            'code': 'in_translation',
            'name': 'Translation in Progress',
            'desc': 'Your documents are being translated by our certified translators.'
        },
        {
            'code': 'quality_approved',
            'name': 'Quality Review',
            'desc': 'The translation is being reviewed and approved by our quality assurance team.'
        },
        {
            'code': 'delivered',
            'name': 'Delivery',
            'desc': 'Your translated documents are being delivered to you. Thank you for choosing our services!'
        },
        {
            'code': 'completed',
            'name': 'Order Completed',
            'desc': 'Your order has been successfully completed. Thank you!'
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
        'order received': 'document_received',
        'order submission stage ( automation email)': 'submitted',
        'state department submission with drop-off/pick-up slip': 'processed_dos',
        'under translation': 'translated',
        'ups label has been generated (automation email)': 'delivered',
        'send review (happy clients) (automation emails)': 'completed',
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
        'in progress ✅ (client agreed to proceed, notarization (if required)': 'notarized',
        'state authentication': 'state_authenticated',
        'federal authentication': 'federal_authenticated',
        'embassy/consulate legalization': 'embassy_legalized',
        'shipping/ drop off': 'delivered',
        'completed ✅ (send review) ( automation email)': 'completed',
    },
    'translation': {
        'client placed request': 'document_received',
        'in progress ✅ (client agreed to proceed, notarization (if required)': 'in_translation',
        'completed': 'quality_approved',
        'shipping/ drop off': 'delivered',
        'completed ✅ (send review)': 'completed',
    },
}


