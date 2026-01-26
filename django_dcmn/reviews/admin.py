from django.contrib import admin
from django.utils.html import format_html
from .models import ReviewRequest


@admin.register(ReviewRequest)
class ReviewRequestAdmin(admin.ModelAdmin):
    list_display = [
        'id', 
        'email', 
        'name', 
        'review_type_badge', 
        'is_sent_badge',
        'leads_won_display',
        'zoho_module',
        'tracking_link',
        'created_at',
        'sent_at',
    ]
    list_filter = ['review_type', 'is_sent', 'created_at', 'zoho_module']
    search_fields = ['email', 'name', 'tracking_id', 'zoho_contact_id', 'zoho_deal_id']
    readonly_fields = [
        'zoho_contact_id', 'zoho_deal_id', 'zoho_module',
        'leads_won_before', 'leads_won_after', 'review_type',
        'created_at', 'sent_at',
        'tracking_link_full',
    ]
    raw_id_fields = ['track']
    date_hierarchy = 'created_at'
    
    fieldsets = (
        ('Customer Info', {
            'fields': ('email', 'name', 'phone')
        }),
        ('Review Details', {
            'fields': ('review_type', 'is_sent')
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
            'fields': ('created_at', 'sent_at'),
            'classes': ('collapse',)
        }),
    )
    
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
