"""Get a Quote v3 intake tables (DESIGN §4.1–4.3).

Allowed values are listed in each field's help_text instead of `choices`, so a later work package can use a
new value without a migration (only `0001_initial` and WP9's `0002_quoteupload` exist for this app).
"""

from django.conf import settings
from django.db import models

GAQL_MODULE = 'Get_A_Quote_Leads'
GAQL_TAB = 'CustomModule7'  # record URL tab of Get_A_Quote_Leads (report 02, DESIGN A10)


class QuoteSubmission(models.Model):
    """One v5 quote request: durable in Postgres before anything external runs."""

    LOCALE_CHOICES = [('en', 'English'), ('es', 'Spanish')]

    reference = models.CharField(max_length=9, unique=True, help_text='Q- + 6 digits, e.g. Q-482913')
    submission_id = models.UUIDField(unique=True)
    schema_version = models.CharField(max_length=16)
    form_version = models.CharField(max_length=40)
    locale = models.CharField(max_length=2, choices=LOCALE_CHOICES, default='en')
    is_test = models.BooleanField(default=False, db_index=True, help_text='full_name starts with "ZZ TEST"')

    # Contact
    full_name = models.CharField(max_length=100)
    email = models.CharField(max_length=254)
    email_normalized = models.CharField(max_length=254, db_index=True, help_text='trimmed, lower-case')
    phone_raw = models.CharField(max_length=40)
    phone_e164 = models.CharField(max_length=20, blank=True, db_index=True)
    phone_last10 = models.CharField(max_length=10, blank=True, db_index=True, help_text='PhoneCallLead convention')
    phone_valid = models.BooleanField(default=False)
    preferred_channel = models.CharField(max_length=10, blank=True, help_text='call|text|whatsapp|email')
    effective_channel = models.CharField(max_length=10, blank=True, help_text='preferred channel after opt-ins')
    when = models.CharField(max_length=10, blank=True, help_text='standard|asap|by_date|unsure')
    deadline = models.DateField(null=True, blank=True)
    message = models.TextField(blank=True)
    terms_method = models.CharField(max_length=20, blank=True)
    terms_version = models.CharField(max_length=40, blank=True)
    consent_text_version = models.CharField(max_length=60, blank=True)
    whatsapp_opt_in = models.BooleanField(default=False)
    sms_opt_in = models.BooleanField(default=False)

    # Request
    primary_service = models.CharField(max_length=20, blank=True)
    service_keys = models.JSONField(default=list, help_text='ordered service keys')
    price_notes = models.JSONField(default=list, blank=True)
    page_url = models.CharField(max_length=1000, blank=True)
    page_path = models.CharField(max_length=255, blank=True)
    referrer_path = models.CharField(max_length=255, blank=True)
    entry = models.JSONField(default=dict, blank=True)
    timing = models.JSONField(default=dict, blank=True)
    client = models.JSONField(default=dict, blank=True)
    attribution_data = models.JSONField(null=True, blank=True, help_text='cleaned attribution')
    raw_payload = models.JSONField(
        null=True, blank=True,
        help_text='as received, minus turnstile_token / website_confirm / fax; superuser only; nulled after 365 days',
    )
    payload_core_hash = models.CharField(max_length=64, db_index=True)
    amendments = models.JSONField(default=list, blank=True)
    contract_warnings = models.JSONField(default=list, blank=True, help_text='dropped keys, defaults applied')
    needs_review = models.BooleanField(default=False)

    # Anti-spam
    ip = models.GenericIPAddressField(null=True, blank=True, help_text='nulled after 90 days')
    user_agent = models.CharField(max_length=500, blank=True, help_text='blanked after 90 days')
    turnstile_result = models.CharField(max_length=8, blank=True, help_text='passed|failed|missing|error|skipped')
    spam_status = models.CharField(
        max_length=10, default='clean', db_index=True, help_text='clean|suspect|held|released|spam',
    )
    spam_reasons = models.JSONField(default=list, blank=True)
    files_dropped = models.BooleanField(default=False)
    possible_repeat_of = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )

    # Zoho
    zoho_module = models.CharField(max_length=64, default=GAQL_MODULE)
    zoho_record_id = models.CharField(max_length=32, blank=True, db_index=True, help_text='GAQL record id')
    zoho_contact_id = models.CharField(max_length=32, blank=True)
    zoho_contact_action = models.CharField(max_length=10, blank=True, help_text='matched|created|skipped')
    zoho_attribution_id = models.CharField(max_length=32, blank=True)
    zoho_note_id = models.CharField(max_length=32, blank=True, help_text='full-receipt Note')
    zoho_lines_mode = models.CharField(max_length=8, blank=True, help_text='text|subform, decided at sync time')
    intake_mode = models.CharField(
        max_length=8, default='none', help_text='primary|none, snapshot of QUOTE_AUTO_INTAKE at save time',
    )

    # Step runner (DESIGN §5.2)
    sync_status = models.CharField(
        max_length=10, default='pending', db_index=True,
        help_text='pending|running|retry|synced|failed|paused|held|spam',
    )
    sync_step = models.CharField(max_length=16, blank=True, help_text='last completed step')
    sync_attempts = models.PositiveSmallIntegerField(default=0)
    sync_next_at = models.DateTimeField(null=True, blank=True)
    sync_lease_until = models.DateTimeField(null=True, blank=True)
    sync_failed_at = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)
    sync_last_error = models.TextField(blank=True, help_text='codes and ids only, ≤2000')

    # Emails
    staff_email_status = models.CharField(
        max_length=8, default='pending', help_text='pending|sending|sent|failed|off|skipped',
    )
    confirmation_email_status = models.CharField(
        max_length=8, default='off', help_text='pending|sending|sent|failed|off|skipped',
    )
    staff_email_sent_at = models.DateTimeField(null=True, blank=True)
    confirmation_email_sent_at = models.DateTimeField(null=True, blank=True)
    created_email_tid = models.CharField(
        max_length=20, blank=True, help_text='TID whose "created" tracking email was queued (at most one)',
    )
    alerted_unsynced_at = models.DateTimeField(null=True, blank=True)
    pii_erased_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Quote — Submission (v3)'
        verbose_name_plural = 'Quote — Submissions (v3)'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['sync_status', 'sync_next_at'], name='quotes_sync_next_idx'),
            models.Index(fields=['spam_status', 'created_at'], name='quotes_spam_created_idx'),
            models.Index(fields=['email_normalized', 'created_at'], name='quotes_email_created_idx'),
            models.Index(fields=['phone_last10', 'created_at'], name='quotes_phone_created_idx'),
            models.Index(fields=['is_test', 'created_at'], name='quotes_test_created_idx'),
        ]

    def __str__(self):
        return self.reference

    @property
    def zoho_url(self) -> str:
        if not self.zoho_record_id:
            return ''
        from crm.zoho import record_url
        return record_url(GAQL_TAB, self.zoho_record_id)

    @property
    def admin_url(self) -> str:
        return f'{settings.BASE_URL}/admin/quotes/quotesubmission/{self.pk}/change/'

    @property
    def display_services(self) -> str:
        """'Apostille + Translation', with the Zoho display values of DESIGN §5.3 (quotes.contract, WP3)."""
        from .contract import SERVICE_DISPLAY
        return ' + '.join(SERVICE_DISPLAY.get(key, key) for key in self.service_keys)


class QuoteLine(models.Model):
    """One requested service of a submission."""

    quote = models.ForeignKey(QuoteSubmission, on_delete=models.CASCADE, related_name='lines')
    position = models.PositiveSmallIntegerField(help_text='1-based')
    service_key = models.CharField(max_length=20)
    source = models.CharField(
        max_length=16, blank=True, help_text='user|picker|url|referrer|guide|addon|tip|route_switch|manager',
    )
    addon_of = models.CharField(max_length=20, blank=True)
    switched_from = models.CharField(max_length=20, blank=True)
    answers = models.JSONField(default=dict, blank=True)
    answers_raw = models.JSONField(default=dict, blank=True)
    answer_source = models.JSONField(default=dict, blank=True)
    dropped_answers = models.JSONField(default=dict, blank=True, help_text='{key: reason}')
    addons = models.JSONField(default=list, blank=True)
    route = models.JSONField(null=True, blank=True)
    price_shown = models.JSONField(null=True, blank=True, help_text='verbatim from the client')
    price_shown_text = models.CharField(max_length=200, blank=True)
    price_kind = models.CharField(max_length=16, blank=True)
    price_flag = models.CharField(max_length=12, blank=True, help_text='approved|review|unapproved|missing')
    summary_client = models.CharField(max_length=300, blank=True)
    summary_server = models.TextField(blank=True, help_text='EN, rendered by quotes/render.py')
    implied_services = models.JSONField(default=list, blank=True)
    needs_review = models.BooleanField(default=False)
    review_reasons = models.JSONField(default=list, blank=True)
    is_primary = models.BooleanField(default=False)
    zoho_row_id = models.CharField(max_length=32, blank=True, help_text='subform row id (phase 2)')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Quote — Line'
        verbose_name_plural = 'Quote — Lines'
        ordering = ['quote', 'position']
        constraints = [
            models.UniqueConstraint(fields=['quote', 'service_key'], name='quotes_unique_line_service'),
            models.UniqueConstraint(fields=['quote', 'position'], name='quotes_unique_line_position'),
        ]

    def __str__(self):
        return f'{self.quote_id}#{self.position} {self.service_key}'


class QuoteRejection(models.Model):
    """A request answered by an anti-spam check; hashes only, purged after 30 days."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    reason = models.CharField(max_length=16, help_text='honeypot|time_trap|turnstile|throttle_email')
    ip_hash = models.CharField(max_length=64, blank=True, help_text='salted SHA-256 (PII_HASH_SALT)')
    email_hash = models.CharField(max_length=64, blank=True, help_text='salted SHA-256 (PII_HASH_SALT)')
    elapsed_ms = models.IntegerField(null=True, blank=True)
    form_version = models.CharField(max_length=40, blank=True)
    page_path = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = 'Quote — Rejection'
        verbose_name_plural = 'Quote — Rejections'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.reason} {self.created_at:%Y-%m-%d %H:%M}'


class QuotePhoneMatch(models.Model):
    """A phone lead matched to a quote; replaces any write to PhoneCallLead.matched_* (invariant 11)."""

    quote = models.ForeignKey(QuoteSubmission, on_delete=models.CASCADE, related_name='phone_matches')
    # No DB constraint and no reverse accessor: deleting a PhoneCallLead behaves exactly as before.
    # Readers use PhoneCallLead.objects.filter(pk=…).first() and tolerate a dangling id.
    phone_lead = models.ForeignKey(
        'orders.PhoneCallLead', on_delete=models.DO_NOTHING, db_constraint=False, related_name='+',
    )
    whatconverts_lead_id = models.CharField(max_length=64, blank=True, help_text='copied, survives a delete')
    direction = models.CharField(max_length=20, help_text='call_before_quote|call_after_quote')
    match_on = models.CharField(max_length=8, help_text='phone|email')
    phone_record_module = models.CharField(max_length=64, blank=True)
    phone_record_id = models.CharField(max_length=32, blank=True)
    action = models.CharField(max_length=10, blank=True, help_text='noted|candidate|linked|ignored')
    note_ids = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Quote — Phone match'
        verbose_name_plural = 'Quote — Phone matches'
        constraints = [
            models.UniqueConstraint(fields=['quote', 'phone_lead'], name='quotes_unique_phone_match'),
        ]

    def __str__(self):
        return f'{self.quote_id} ↔ phone lead {self.phone_lead_id}'


class ShadowRun(models.Model):
    """S1 shadow mapping of a legacy quote: read-only Zoho GETs, no PII values stored."""

    quote_request = models.ForeignKey(
        'orders.QuoteRequest', on_delete=models.DO_NOTHING, db_constraint=False, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    gaql_record_id = models.CharField(max_length=32, blank=True)
    timeline_copy = models.JSONField(null=True, blank=True, help_text='{module, id} or null')
    heuristic_copy = models.JSONField(null=True, blank=True)
    agree = models.BooleanField(null=True, blank=True)
    schema_warnings = models.JSONField(default=list, blank=True)
    payload_preview = models.JSONField(default=dict, blank=True, help_text='field names and lengths only')
    error = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Quote — Shadow run'
        verbose_name_plural = 'Quote — Shadow runs'
        ordering = ['-created_at']

    def __str__(self):
        return f'shadow quote request {self.quote_request_id}'
