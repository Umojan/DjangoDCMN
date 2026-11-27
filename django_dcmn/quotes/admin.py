"""Read-only admin of the v3 intake tables (DESIGN §12.3).

Nothing is edited by hand here: every change goes through an action. WP4 adds the actions (Retry from failed
step, Release from hold, Mark spam, Resend staff email, Resend confirmation, Re-run intake, Erase PII) in
`QuoteSubmissionActionsMixin` below. `raw_payload` is shown to superusers only.
"""

from django.contrib import admin
from django.utils.html import format_html

from crm.models import ServiceLink

from .models import QuotePhoneMatch, QuoteLine, QuoteRejection, QuoteSubmission, ShadowRun
from .render import comments_text, lines_text, receipt_text


class ReadOnlyAdminMixin:
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class QuoteLineInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = QuoteLine
    extra = 0
    can_delete = False
    fields = ('position', 'service_key', 'source', 'addon_of', 'is_primary', 'price_flag', 'summary_server',
              'answers', 'dropped_answers', 'needs_review', 'review_reasons')
    readonly_fields = fields
    ordering = ('position',)


class ServiceLinkInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = ServiceLink
    fk_name = 'quote'
    extra = 0
    can_delete = False
    fields = ('service_key', 'state', 'origin', 'target_module', 'target_record_id', 'tracking_id',
              'last_error_code', 'updated_at')
    readonly_fields = fields


class QuoteSubmissionActionsMixin:
    """The admin actions of DESIGN §12.3; each one is idempotent and reports what it changed."""

    actions = ('retry_from_failed_step', 'release_from_hold', 'mark_spam', 'resend_staff_email',
               'resend_confirmation', 'rerun_intake', 'erase_pii')

    @admin.action(description='Retry from failed step')
    def retry_from_failed_step(self, request, queryset):
        from . import pipeline

        self._run_action(request, queryset, pipeline.rearm, 'Re-armed')

    @admin.action(description='Release from hold')
    def release_from_hold(self, request, queryset):
        from . import pipeline

        self._run_action(request, queryset, pipeline.release, 'Released')

    @admin.action(description='Mark spam (only quotes not in Zoho)')
    def mark_spam(self, request, queryset):
        from . import pipeline

        self._run_action(request, queryset, pipeline.mark_spam, 'Marked spam')

    @admin.action(description='Resend staff email')
    def resend_staff_email(self, request, queryset):
        from . import pipeline

        self._run_action(request, queryset, lambda quote: pipeline.resend_email(quote, 'staff'), 'Queued')

    @admin.action(description='Resend confirmation')
    def resend_confirmation(self, request, queryset):
        from . import pipeline

        self._run_action(request, queryset, lambda quote: pipeline.resend_email(quote, 'confirmation'), 'Queued')

    @admin.action(description='Re-run intake')
    def rerun_intake(self, request, queryset):
        from . import pipeline

        self._run_action(request, queryset, pipeline.rerun_intake, 'Intake re-run for')

    @admin.action(description='Erase PII (superuser)', permissions=['erase'])
    def erase_pii(self, request, queryset):
        from .tasks.maintenance import erase_quote_pii

        self._run_action(request, queryset, lambda quote: erase_quote_pii(quote) or True, 'Erased')

    def has_erase_permission(self, request):
        return request.user.is_superuser

    def _run_action(self, request, queryset, action, verb):
        from django.contrib import messages

        done = sum(bool(action(quote)) for quote in queryset)
        unchanged = len(queryset) - done
        text = f'{verb} {done} quote(s).' + (f' {unchanged} not in a state for this action.' if unchanged else '')
        self.message_user(request, text, messages.SUCCESS if done else messages.WARNING)


@admin.register(QuoteSubmission)
class QuoteSubmissionAdmin(QuoteSubmissionActionsMixin, ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('reference', 'created_at', 'full_name', 'display_services', 'spam_status', 'sync_status',
                    'sync_step', 'staff_email_status', 'confirmation_email_status', 'zoho_link', 'is_test')
    list_filter = ('spam_status', 'sync_status', 'staff_email_status', 'confirmation_email_status', 'is_test',
                   'intake_mode', 'needs_review')
    search_fields = ('reference', 'email_normalized', 'phone_last10', 'full_name', 'zoho_record_id')
    date_hierarchy = 'created_at'
    inlines = (QuoteLineInline, ServiceLinkInline)
    fieldsets = (
        (None, {'fields': ('reference', 'submission_id', 'created_at', 'is_test', 'locale', 'form_version',
                           'schema_version', 'needs_review', 'contract_warnings')}),
        ('Contact', {'fields': ('full_name', 'email', 'phone_raw', 'phone_e164', 'phone_valid',
                                'preferred_channel', 'effective_channel', 'whatsapp_opt_in', 'sms_opt_in',
                                'when', 'deadline', 'message', 'terms_method', 'terms_version',
                                'consent_text_version')}),
        ('Request', {'fields': ('primary_service', 'service_keys', 'price_notes', 'page_url', 'referrer_path',
                                'entry', 'timing', 'client', 'attribution_data', 'files_dropped', 'amendments')}),
        ('Anti-spam', {'fields': ('spam_status', 'spam_reasons', 'turnstile_result', 'possible_repeat_of', 'ip',
                                  'user_agent')}),
        ('Zoho', {'fields': ('zoho_link', 'zoho_record_id', 'zoho_contact_id', 'zoho_contact_action',
                             'zoho_attribution_id', 'zoho_note_id', 'zoho_lines_mode', 'intake_mode')}),
        ('Sync', {'fields': ('sync_status', 'sync_step', 'sync_attempts', 'sync_next_at', 'sync_failed_at',
                             'synced_at', 'sync_last_error', 'staff_email_status', 'staff_email_sent_at',
                             'confirmation_email_status', 'confirmation_email_sent_at', 'pii_erased_at')}),
        ('Texts', {'fields': ('comments_preview', 'lines_preview', 'receipt_preview')}),
    )

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if request.user.is_superuser:
            fieldsets = (*fieldsets, ('Raw payload (superuser only)', {'fields': ('raw_payload',)}))
        return fieldsets

    def get_readonly_fields(self, request, obj=None):
        return [field for _name, options in self.get_fieldsets(request, obj) for field in options['fields']]

    @admin.display(description='Zoho')
    def zoho_link(self, obj):
        return format_html('<a href="{}" target="_blank">open</a>', obj.zoho_url) if obj.zoho_url else '—'

    @admin.display(description='Client_Comments')
    def comments_preview(self, obj):
        return format_html('<pre style="white-space:pre-wrap">{}</pre>', comments_text(obj))

    @admin.display(description='Quote_Lines_Text')
    def lines_preview(self, obj):
        return format_html('<pre style="white-space:pre-wrap">{}</pre>', lines_text(obj))

    @admin.display(description='Receipt Note')
    def receipt_preview(self, obj):
        return format_html('<pre style="white-space:pre-wrap">{}</pre>', receipt_text(obj))


@admin.register(QuoteRejection)
class QuoteRejectionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('created_at', 'reason', 'elapsed_ms', 'form_version', 'page_path')
    list_filter = ('reason',)
    readonly_fields = ('created_at', 'reason', 'ip_hash', 'email_hash', 'elapsed_ms', 'form_version', 'page_path')


@admin.register(QuotePhoneMatch)
class QuotePhoneMatchAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('quote', 'phone_lead_id', 'direction', 'match_on', 'action', 'created_at')
    list_filter = ('direction', 'match_on', 'action')
    readonly_fields = ('quote', 'phone_lead_id', 'whatconverts_lead_id', 'direction', 'match_on',
                       'phone_record_module', 'phone_record_id', 'action', 'note_ids', 'created_at')
    exclude = ('phone_lead',)


@admin.register(ShadowRun)
class ShadowRunAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('quote_request_id', 'created_at', 'gaql_record_id', 'agree', 'error')
    list_filter = ('agree',)
    readonly_fields = ('quote_request_id', 'created_at', 'gaql_record_id', 'timeline_copy', 'heuristic_copy',
                       'agree', 'schema_warnings', 'payload_preview', 'error')
    exclude = ('quote_request',)
