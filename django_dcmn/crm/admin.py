"""Admin of the engine tables (DESIGN §12.3).

Links change only through actions (Retry, Dismiss, Re-run write-back); engine requests and schema snapshots are
read-only and append-only; signals can be re-evaluated; a rule may be set to `create` only with an approval.
"""

from django.contrib import admin, messages
from django.db import transaction
from django.utils import timezone
from django.utils.html import format_html

from . import rules
from .models import AutomationRule, EngineRequest, ServiceLink, ServiceSignal, ZohoSchemaSnapshot

RETRYABLE_STATES = ('failed', 'degraded', 'retry', 'pending', 'needs_decision', 'target_deleted')


class ReadOnlyAdminMixin:
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def _link(url: str) -> str:
    return format_html('<a href="{}" target="_blank" rel="noopener">open</a>', url) if url else '—'


@admin.register(ServiceLink)
class ServiceLinkAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('external_key', 'state', 'origin', 'target_module', 'target', 'attempts', 'last_error_code',
                    'trigger', 'updated_at')
    list_filter = ('state', 'origin', 'service_key', 'source_kind', 'trigger')
    search_fields = ('external_key', 'source_key', 'target_record_id', 'tracking_id')
    readonly_fields = [f.name for f in ServiceLink._meta.fields]
    actions = ('retry', 'dismiss', 'rerun_writeback')

    @admin.display(description='Zoho record')
    def target(self, obj):
        return _link(obj.target_url)

    @admin.action(description='Retry (state → pending, run now)')
    def retry(self, request, queryset):
        from .tasks import run_service_link

        links = list(queryset.filter(state__in=RETRYABLE_STATES))
        if not links:
            self.message_user(request, 'No link in a retryable state.', messages.WARNING)
            return
        audit = EngineRequest.objects.create(caller='admin', trigger='admin_retry', mode='create', state='running',
                                             actor={'staff_user_id': request.user.pk},
                                             services_requested=sorted({link.service_key for link in links}))
        for link in links:
            overrides = dict(link.overrides or {})
            if link.state == 'needs_decision':
                overrides['create_new'] = True   # a retry of a decision means "create new"
            ServiceLink.objects.filter(pk=link.pk).update(
                state='pending', attempts=0, next_attempt_at=None, lease_until=None, overrides=overrides,
                request=audit, updated_at=timezone.now())
            transaction.on_commit(lambda link_id=link.pk: run_service_link.delay(link_id))
        self.message_user(request, f'{len(links)} link(s) queued ({audit.ref}).')

    @admin.action(description='Dismiss (no Zoho change)')
    def dismiss(self, request, queryset):
        from .tasks import writeback_source

        links = list(queryset.exclude(state__in=('created', 'adopted', 'creating', 'dismissed')))
        for link in links:
            ServiceLink.objects.filter(pk=link.pk).update(state='dismissed', updated_at=timezone.now())
        for source_key in {link.source_key for link in links}:
            transaction.on_commit(lambda key=source_key: writeback_source.delay(key))
        self.message_user(request, f'{len(links)} link(s) dismissed.')

    @admin.action(description='Re-run write-back')
    def rerun_writeback(self, request, queryset):
        from .tasks import writeback_source

        source_keys = sorted(set(queryset.values_list('source_key', flat=True)))
        for source_key in source_keys:
            transaction.on_commit(lambda key=source_key: writeback_source.delay(key))
        self.message_user(request, f'Write-back queued for {len(source_keys)} source(s).')


@admin.register(EngineRequest)
class EngineRequestAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('created_at', 'ref', 'caller', 'trigger', 'mode', 'source_key', 'state', 'http_status',
                    'error_code', 'duration_ms')
    list_filter = ('state', 'caller', 'trigger', 'mode', 'http_status')
    search_fields = ('public_id', 'client_request_id', 'source_key', 'source_record_id', 'caller_key_id')
    readonly_fields = [f.name for f in EngineRequest._meta.fields]
    date_hierarchy = 'created_at'


@admin.register(ServiceSignal)
class ServiceSignalAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('dedupe_key', 'kind', 'service_key', 'state', 'outcome', 'rule_key', 'attempts', 'created_at')
    list_filter = ('kind', 'state', 'outcome')
    search_fields = ('dedupe_key', 'source_key', 'source_record_id')
    readonly_fields = [f.name for f in ServiceSignal._meta.fields]
    actions = ('re_evaluate',)

    @admin.action(description='Re-evaluate')
    def re_evaluate(self, request, queryset):
        outcomes = [rules.evaluate(signal) for signal in queryset]
        self.message_user(request, f'{len(outcomes)} signal(s) re-evaluated: {", ".join(sorted(set(outcomes)))}.'
                          if outcomes else 'Nothing selected.')


@admin.register(AutomationRule)
class AutomationRuleAdmin(admin.ModelAdmin):
    """`create` needs approved_by and approved_at (AutomationRule.clean); the effective mode is capped by
    CRM_RULES_MAX_MODE."""

    list_display = ('key', 'mode', 'effective', 'approved_by', 'approved_at', 'updated_at')
    fields = ('key', 'mode', 'conditions', 'approved_by', 'approved_at')

    @admin.display(description='effective mode')
    def effective(self, obj):
        return rules.effective_mode(obj.key, alert=False)


@admin.register(ZohoSchemaSnapshot)
class ZohoSchemaSnapshotAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('module', 'fetched_at', 'sha256')
    list_filter = ('module',)
    readonly_fields = ('module', 'fetched_at', 'sha256', 'fields')
