STAGE_DEFS = {
    'fbi_apostille': [
        {
            'code': 'document_received',
            'name': 'Document Received',
            'desc': 'We have received your documents and are preparing them for the next stage of processing.'
        },
        {
            'code': 'notarized',
            'name': 'Notarization',
            'desc': 'Your documents are undergoing notary verification by a certified notary public.'
        },
        {
            'code': 'submitted',
            'name': 'State Submission',
            'desc': 'Your documents are being submitted to the state authority for authentication.'
        },
        {
            'code': 'processed_dos',
            'name': 'U.S. DoS Processing',
            'desc': 'Your documents are under review at the U.S. Department of State for federal authentication. Our liaison is monitoring the process to ensure timely completion.'
        },
        {
            'code': 'translated',
            'name': 'Translation',
            'desc': 'Your documents are being translated by certified translators to meet the requirements of the destination country.'
        },
        {
            'code': 'delivered',
            'name': 'Delivery',
            'desc': 'Your documents are ready for final delivery. Thank you for choosing our services!'
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
            'name': 'Notarization',
            'desc': 'Your documents are undergoing notary verification by a certified notary public.'
        },
        {
            'code': 'submitted',
            'name': 'State Submission',
            'desc': 'Your documents are being submitted to the state authority for apostille certification.'
        },
        {
            'code': 'processed_state',
            'name': 'State Processing',
            'desc': 'Your documents are being reviewed and processed by the state authority. We are monitoring the progress to ensure timely completion.'
        },
        {
            'code': 'delivered',
            'name': 'Delivery',
            'desc': 'Your documents are ready for final delivery. Thank you for choosing our services!'
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
            'name': 'Notarization',
            'desc': 'Your documents are undergoing notary verification by a certified notary public.'
        },
        {
            'code': 'state_authenticated',
            'name': 'State Authentication',
            'desc': 'Your documents are being authenticated by the state authority as part of the legalization process.'
        },
        {
            'code': 'federal_authenticated',
            'name': 'U.S. DoS Authentication',
            'desc': 'Your documents are being authenticated by the U.S. Department of State before embassy legalization.'
        },
        {
            'code': 'embassy_legalized',
            'name': 'Embassy / Consulate Legalization',
            'desc': 'Your documents are undergoing legalization at the embassy or consulate of the destination country.'
        },
        {
            'code': 'translated',
            'name': 'Translation',
            'desc': 'Your documents are being translated by certified translators to meet the requirements of the destination country.'
        },
        {
            'code': 'delivered',
            'name': 'Delivery',
            'desc': 'Your documents are ready for final delivery. Thank you for choosing our services!'
        },
    ],

    'translation': [
        {
            'code': 'document_received',
            'name': 'Document Received',
            'desc': 'We have received your documents and are preparing them for translation.'
        },
        {
            'code': 'translated',
            'name': 'Translation',
            'desc': 'Your documents are being translated by our certified translators.'
        },
        {
            'code': 'quality_approved',
            'name': 'Quality Review',
            'desc': 'Your translation is undergoing quality assurance review to ensure accuracy and compliance.'
        },
        {
            'code': 'delivered',
            'name': 'Delivery',
            'desc': 'Your translated documents are ready for final delivery. Thank you for choosing our services!'
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
        'pending submission': 'submitted',
        'order submission stage ( automation email)': 'submitted',
        'state department submission with drop-off/pick-up slip': 'submitted',
        'pick-up of documents from the state department': 'processed_dos',
        'ups label has been generated (automation email)': 'delivered',
        'resubmissions on company cost': 'submitted',
        'rejected': 'document_received',
        'send review (happy clients) (automation emails)': 'delivered',
        'no review ( unhappy client)': 'delivered',
        'documents dropped off at ups store or client’s address': 'delivered',
        'under translation': 'translated',
        'no label / not yet dropped off': 'delivered',
        'fully refunded ( cancelled orders)': 'delivered',
        'from apostille request': 'document_received',
        'notarization': 'notarized',
        'court': 'notarized',
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
        'client placed request': 'document_received',
        'document received': 'document_received',
        'in translation': 'translated',
        'under review': 'quality_approved',
        'delivered': 'delivered',
    },
}
