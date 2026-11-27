"""Related-service engine tables (DESIGN §4.4).

Allowed values are listed in help_text instead of `choices` (except AutomationRule.mode, edited in the admin),
so later work packages need no migration: `crm` has exactly one, `0001_initial`.
"""

import uuid

from django.core.exceptions import ValidationError
from django.db import models


class EngineRequest(models.Model):
    """One engine call: the audit log, the job polled by the panel and the retried-click guard."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, help_text='job_id in API responses')
    client_request_id = models.CharField(max_length=140, blank=True, db_index=True, help_text='recordId:serviceKey')
    caller = models.CharField(max_length=12, help_text='system|zoho|panel|staff|gmail|rules|admin')
    trigger = models.CharField(max_length=40, blank=True)
    mode = models.CharField(max_length=8, help_text='preview|suggest|create|dry_run')
    actor = models.JSONField(default=dict, blank=True)
    caller_key_id = models.CharField(max_length=8, blank=True, help_text='sha256(key)[:8], never the key')
    source_key = models.CharField(max_length=80, blank=True, db_index=True)
    source_module = models.CharField(max_length=64, blank=True, db_index=True)
    source_record_id = models.CharField(max_length=32, blank=True, db_index=True)
    services_requested = models.JSONField(default=list, blank=True, help_text='["*"] or service keys')
    state = models.CharField(
        max_length=14, default='accepted', help_text='accepted|running|done|partial|needs_decision|failed|refused',
    )
    result = models.JSONField(null=True, blank=True, help_text='response body, redacted by crm/redact.py')
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True, help_text='nulled after 90 days')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'CRM — Engine request'
        verbose_name_plural = 'CRM — Engine requests'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.ref} {self.caller} {self.state}'

    @property
    def ref(self) -> str:
        """Short id shown to managers, e.g. ENG-0B5C6A8E."""
        return f'ENG-{self.public_id.hex[:8].upper()}'


class ServiceLink(models.Model):
    """The engine's unit of work and idempotency anchor: one row per (source, service, seq)."""

    source_kind = models.CharField(max_length=8, help_text='quote|zoho|form')
    source_key = models.CharField(max_length=80, db_index=True, help_text='qr:57 · rel:gaql:<id> · form:apostille:943')
    source_module = models.CharField(max_length=64, blank=True)
    source_record_id = models.CharField(max_length=32, blank=True, db_index=True)
    quote = models.ForeignKey(
        'quotes.QuoteSubmission', null=True, blank=True, on_delete=models.SET_NULL, related_name='service_links',
    )
    quote_line = models.ForeignKey(
        'quotes.QuoteLine', null=True, blank=True, on_delete=models.SET_NULL, related_name='service_links',
    )
    service_key = models.CharField(max_length=24, help_text='registry key')
    seq = models.PositiveSmallIntegerField(default=1, help_text='2+ only after "create again"')
    external_key = models.CharField(max_length=100, unique=True, help_text='dcmn:{source_key}:{service_key}[#seq]')
    state = models.CharField(
        max_length=16, default='pending', db_index=True,
        help_text='suggested|dismissed|pending|creating|retry|created|adopted|needs_decision|degraded|failed|'
                  'target_deleted',
    )
    origin = models.CharField(
        max_length=16, blank=True, help_text='created|adopted_key|adopted_copy|adopted_manual|adopted_phone',
    )
    reason = models.CharField(max_length=60, blank=True, help_text='requested|intake|implied:…|signal:…')
    target_module = models.CharField(max_length=64, blank=True)
    target_record_id = models.CharField(max_length=32, blank=True, db_index=True)
    target_url = models.CharField(max_length=255, blank=True)
    target_key_written = models.BooleanField(default=False)
    tracking_id = models.CharField(max_length=20, blank=True, help_text='minted silently (DESIGN §7.12)')
    trigger = models.CharField(max_length=40, blank=True)
    caller = models.CharField(max_length=12, blank=True)
    actor = models.JSONField(default=dict, blank=True, help_text='{kind, zoho_user_id, email, initials, staff_user_id}')
    overrides = models.JSONField(default=dict, blank=True, help_text='panel only')
    candidates = models.JSONField(default=list, blank=True, help_text='[{module, id, name, stage, created, url, match}]')
    payload_sha256 = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, help_text='no PII')
    last_error_code = models.CharField(max_length=40, blank=True)
    writeback = models.JSONField(default=dict, blank=True, help_text='{target_note, source_note, source_lines}')
    request = models.ForeignKey(
        EngineRequest, null=True, blank=True, on_delete=models.SET_NULL, related_name='links',
        help_text='the request that last acted on it',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'CRM — Service link'
        verbose_name_plural = 'CRM — Service links'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['source_key', 'service_key', 'seq'], name='crm_unique_service_link'),
        ]
        indexes = [
            models.Index(fields=['state', 'next_attempt_at'], name='crm_link_state_next_idx'),
            models.Index(fields=['target_module', 'target_record_id'], name='crm_link_target_idx'),
            models.Index(fields=['source_module', 'source_record_id'], name='crm_link_source_idx'),
            models.Index(fields=['quote', 'service_key'], name='crm_link_quote_service_idx'),
        ]

    def __str__(self):
        return f'{self.external_key} [{self.state}]'


class ServiceSignal(models.Model):
    """Something implies service S: the translation checkbox, an implied apostille; later Zoho events, Square."""

    dedupe_key = models.CharField(max_length=160, unique=True, help_text='{kind}:{source_key}:{service_key}')
    kind = models.CharField(max_length=32, help_text='translation_requested|apostille_implied|…')
    source_kind = models.CharField(max_length=8, help_text='quote|zoho|form')
    source_key = models.CharField(max_length=80, db_index=True)
    source_module = models.CharField(max_length=64, blank=True)
    source_record_id = models.CharField(max_length=32, blank=True)
    order_type = models.CharField(max_length=16, blank=True, help_text='legacy form order type')
    order_id = models.BigIntegerField(null=True, blank=True)
    quote = models.ForeignKey(
        'quotes.QuoteSubmission', null=True, blank=True, on_delete=models.CASCADE, related_name='service_signals',
    )
    service_key = models.CharField(max_length=24)
    data = models.JSONField(default=dict, blank=True)
    state = models.CharField(
        max_length=16, default='new', db_index=True,
        help_text='waiting_source|new|fields_written|evaluated|expired|error',
    )
    outcome = models.CharField(max_length=12, blank=True, help_text='dry_run|suggested|created|ignored|note_only')
    rule_key = models.CharField(max_length=60, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    evaluated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'CRM — Service signal'
        verbose_name_plural = 'CRM — Service signals'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.dedupe_key} [{self.state}]'


class AutomationRule(models.Model):
    """Rule policy override; the table starts empty and crm.rules.RULE_DEFAULTS applies."""

    MODE_CHOICES = [('off', 'Off'), ('dry_run', 'Dry run'), ('suggest', 'Suggest'), ('create', 'Create')]

    key = models.CharField(max_length=60, unique=True)
    mode = models.CharField(max_length=8, choices=MODE_CHOICES, default='off')
    conditions = models.JSONField(default=dict, blank=True)
    approved_by = models.CharField(max_length=100, blank=True, help_text='required for "create"')
    approved_at = models.DateTimeField(null=True, blank=True, help_text='required for "create"')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'CRM — Automation rule'
        verbose_name_plural = 'CRM — Automation rules'
        ordering = ['key']

    def __str__(self):
        return f'{self.key}: {self.mode}'

    def clean(self):
        if self.mode == 'create' and not (self.approved_by and self.approved_at):
            raise ValidationError({'approved_by': 'A rule may create records only with a recorded approval.'})


class ZohoSchemaSnapshot(models.Model):
    """Last-known-good Zoho field metadata of one module (the latest row per module wins)."""

    module = models.CharField(max_length=64, db_index=True)
    fetched_at = models.DateTimeField()
    fields = models.JSONField(default=list, help_text='[{api_name, data_type, length, read_only, pick_list_values}]')
    sha256 = models.CharField(max_length=64)

    class Meta:
        verbose_name = 'CRM — Zoho schema snapshot'
        verbose_name_plural = 'CRM — Zoho schema snapshots'
        ordering = ['-fetched_at']
        get_latest_by = 'fetched_at'

    def __str__(self):
        return f'{self.module} @ {self.fetched_at:%Y-%m-%d %H:%M}'
