from django.contrib import admin
from .models import FbiApostilleOrder, OrderFile


class OrderFileInline(admin.TabularInline):
    model = OrderFile
    extra = 0


@admin.register(FbiApostilleOrder)
class FbiApostilleOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'package', 'count', 'shipping_option', 'total_price', 'is_paid', 'created_at')
    list_filter = ('package', 'shipping_option', 'is_paid', 'created_at')
    search_fields = ('name', 'email', 'country_name', 'address')
    inlines = [OrderFileInline]


@admin.register(OrderFile)
class OrderFileAdmin(admin.ModelAdmin):
    list_display = ('id', 'order', 'file', 'uploaded_at')
    search_fields = ('order__name',)