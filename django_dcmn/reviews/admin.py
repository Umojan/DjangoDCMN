from django.contrib import admin
from django.utils.html import format_html
from .models import Review, ReviewRequest


@admin.register(ReviewRequest)
class ReviewRequestAdmin(admin.ModelAdmin):
    list_display = [
        'id', 
        'email', 
        'name', 
        'review_type_badge', 
        'is_sent_badge',
        'rating_display',
        'leads_won_display',
        'zoho_module',
        'tracking_link',
        'created_at',
        'sent_at',
    ]
    list_filter = ['review_type', 'is_sent', 'stars_email_sent', 'created_at', 'zoho_module']
    search_fields = ['email', 'name', 'tracking_id', 'zoho_contact_id', 'zoho_deal_id']
    readonly_fields = [
        'zoho_contact_id', 'zoho_deal_id', 'zoho_module',
        'leads_won_before', 'leads_won_after', 'review_type',
        'created_at', 'sent_at', 'reminder_sent_at', 'trustpilot_invite_sent_at', 'stars_email_sent',
        'tracking_link_full', 'rating_display', 'rate_links',
    ]
    raw_id_fields = ['track']
    date_hierarchy = 'created_at'
    
    fieldsets = (
        ('Customer Info', {
            'fields': ('email', 'name', 'phone')
        }),
        ('Review Details', {
            'fields': ('review_type', 'is_sent', 'stars_email_sent', 'rating_display', 'rate_links')
        }),
        ('Leads Won', {
            'fields': ('leads_won_before', 'leads_won_after'),
            'description': 'Leads Won value in Zoho Contacts before and after processing'
        }),
        ('Zoho References', {
            'fields': ('zoho_contact_id', 'zoho_deal_id', 'zoho_module'),
            'classes': ('collapse',)
        }),
        ('Tracking', {
            'fields': ('track', 'tracking_id', 'tracking_link_full')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'sent_at', 'reminder_sent_at', 'trustpilot_invite_sent_at'),
            'classes': ('collapse',)
        }),
    )

    def rating_display(self, obj):
        review = getattr(obj, 'review', None)
        if not review or not review.rating:
            return format_html('<span style="color:#aaa;">—</span>')
        color = '#28a745' if review.is_positive else '#dc3545'
        return format_html(
            '<a href="/admin/reviews/review/{}/change/" style="color:{}; font-weight:bold; text-decoration:none;" title="{}">{}</a>',
            review.id, color, review.route, review.stars
        )
    rating_display.short_description = 'Rating'

    def rate_links(self, obj):
        from .services import star_links
        if not obj.pk:
            return '-'
        return format_html(
            ' '.join(f'<a href="{s["url"]}" target="_blank">{s["stars"]}★</a>' for s in star_links(obj))
        )
    rate_links.short_description = 'Star links (for testing)'
    
    def review_type_badge(self, obj):
        if not obj.review_type:
            return format_html(
                '<span style="background-color: #6c757d; color: white; padding: 3px 8px; '
                'border-radius: 3px; font-size: 11px;">NEW</span>'
            )
        colors = {
            'google': '#4285f4',      # Google blue
            'trustpilot': '#00b67a',  # TrustPilot green
        }
        color = colors.get(obj.review_type, '#666')
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; '
            'border-radius: 3px; font-size: 11px; font-weight: bold;">{}</span>',
            color, obj.review_type.upper()
        )
    review_type_badge.short_description = 'Type'
    
    def is_sent_badge(self, obj):
        if obj.is_sent:
            return format_html(
                '<span style="background-color: #28a745; color: white; padding: 3px 8px; '
                'border-radius: 3px; font-size: 11px;">✅ SENT</span>'
            )
        return format_html(
            '<span style="background-color: #ffc107; color: #333; padding: 3px 8px; '
            'border-radius: 3px; font-size: 11px;">⏳ PENDING</span>'
        )
    is_sent_badge.short_description = 'Sent'
    is_sent_badge.admin_order_field = 'is_sent'
    
    def leads_won_display(self, obj):
        return format_html(
            '<span title="Before → After">{} → {}</span>',
            obj.leads_won_before, obj.leads_won_after
        )
    leads_won_display.short_description = 'Leads Won'
    
    def tracking_link(self, obj):
        if obj.tracking_id:
            url = obj.tracking_url
            if url:
                return format_html(
                    '<a href="{}" target="_blank" title="Open tracking page">'
                    '🔗 {}</a>',
                    url, obj.tracking_id
                )
            return obj.tracking_id
        return '-'
    tracking_link.short_description = 'Tracking'
    
    def tracking_link_full(self, obj):
        if obj.tracking_url:
            return format_html(
                '<a href="{}" target="_blank">{}</a>',
                obj.tracking_url, obj.tracking_url
            )
        return '-'
    tracking_link_full.short_description = 'Tracking URL'


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'stars_badge', 'route_badge', 'customer', 'service', 'tracking', 'feedback_short',
        'callback_requested', 'manager_notified', 'zoho_status', 'rated_at', 'feedback_at',
    ]
    list_filter = ['route', 'rating', 'callback_requested', 'manager_notified', 'rated_at']
    search_fields = ['request__email', 'request__name', 'request__tracking_id', 'feedback_text']
    readonly_fields = [
        'request', 'rating', 'route', 'rated_at', 'feedback_at', 'manager_notified', 'notified_with_feedback',
        'zoho_note_id', 'zoho_task_id', 'zoho_error', 'ip', 'user_agent', 'created_at', 'updated_at', 'zoho_link',
    ]
    date_hierarchy = 'rated_at'
    fieldsets = (
        ('Rating', {'fields': ('request', 'rating', 'route', 'rated_at')}),
        ('Feedback', {'fields': ('feedback_text', 'callback_requested', 'callback_phone', 'feedback_at')}),
        ('Managers / Zoho', {'fields': ('manager_notified', 'notified_with_feedback', 'zoho_link', 'zoho_note_id', 'zoho_task_id', 'zoho_error')}),
        ('Meta', {'fields': ('ip', 'user_agent', 'created_at', 'updated_at'), 'classes': ('collapse',)}),
    )
    actions = ['resend_manager_alert']

    def stars_badge(self, obj):
        color = '#28a745' if obj.is_positive else '#dc3545'
        return format_html('<span style="color:{}; font-size:15px; letter-spacing:1px;">{}</span>', color, obj.stars)
    stars_badge.short_description = 'Rating'
    stars_badge.admin_order_field = 'rating'

    def route_badge(self, obj):
        colors = {'google': '#4285f4', 'trustpilot': '#00b67a', 'internal': '#dc3545'}
        return format_html(
            '<span style="background:{}; color:#fff; padding:3px 8px; border-radius:3px; font-size:11px; font-weight:bold;">{}</span>',
            colors.get(obj.route, '#666'), obj.get_route_display().upper()
        )
    route_badge.short_description = 'Routed to'
    route_badge.admin_order_field = 'route'

    def customer(self, obj):
        return f"{obj.request.name or ''} <{obj.request.email}>".strip()

    def service(self, obj):
        from .tasks import _get_service_label
        return _get_service_label(obj.request.zoho_module)

    def tracking(self, obj):
        return obj.request.tracking_id or '-'

    def feedback_short(self, obj):
        t = (obj.feedback_text or '').strip()
        return (t[:80] + '…') if len(t) > 80 else (t or '—')
    feedback_short.short_description = 'Feedback'

    def zoho_status(self, obj):
        if obj.is_positive:
            return '—'
        if obj.zoho_error:
            return format_html('<span style="color:#dc3545;" title="{}">⚠ error</span>', obj.zoho_error[:200])
        if obj.zoho_task_id:
            return format_html('<span style="color:#28a745;">✅ note + task</span>')
        if obj.zoho_note_id:
            return format_html('<span style="color:#28a745;">✅ note</span>')
        return format_html('<span style="color:#999;">⏳</span>')
    zoho_status.short_description = 'Zoho'

    def zoho_link(self, obj):
        from .services import zoho_record_url
        url = zoho_record_url(obj.request)
        return format_html('<a href="{}" target="_blank">{}</a>', url, url) if url else '-'
    zoho_link.short_description = 'Zoho record'

    @admin.action(description='Re-send manager alert (+ Zoho note/task) for selected negative reviews')
    def resend_manager_alert(self, request, queryset):
        from .services import send_manager_alert, push_negative_to_zoho
        n = 0
        for review in queryset.filter(route=Review.ROUTE_INTERNAL):
            send_manager_alert(review, update=bool(review.feedback_text))
            push_negative_to_zoho(review)
            n += 1
        self.message_user(request, f'Re-sent {n} alert(s).')
