from django.db import models
from django.conf import settings


class ReviewRequest(models.Model):
    """Запись о запросе отзыва от клиента."""
    
    REVIEW_TYPE_CHOICES = [
        ('google', 'Google Review'),
        ('trustpilot', 'TrustPilot'),
    ]
    
    # Данные клиента
    email = models.EmailField(db_index=True)
    name = models.CharField(max_length=255, blank=True, default='')
    phone = models.CharField(max_length=50, blank=True, default='')
    
    # Zoho references
    zoho_contact_id = models.CharField(max_length=50, db_index=True)
    zoho_deal_id = models.CharField(max_length=50, blank=True, default='')
    zoho_module = models.CharField(
        max_length=100, 
        blank=True, 
        default='',
        help_text="Zoho module name (Deals, Triple_Seal_Apostilles, etc.)"
    )
    
    # Связь с Track
    track = models.ForeignKey(
        'orders.Track', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='review_requests'
    )
    tracking_id = models.CharField(max_length=20, blank=True, default='', db_index=True)
    
    # Данные review
    review_type = models.CharField(
        max_length=20, 
        choices=REVIEW_TYPE_CHOICES,
        blank=True,
        default='',
        help_text="Определяется автоматически на основе Leads Won"
    )
    leads_won_before = models.IntegerField(default=0, help_text="Leads Won до обновления")
    leads_won_after = models.IntegerField(default=0, help_text="Leads Won после обновления")
    
    # Статус
    is_sent = models.BooleanField(default=False, help_text="Был ли отправлен review request")
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        verbose_name = '📝 Review Request'
        verbose_name_plural = '📝 Review Requests'
        ordering = ['-created_at']
    
    def __str__(self):
        type_str = self.review_type.upper() if self.review_type else 'NEW'
        status = '✅' if self.is_sent else '⏳'
        return f"{status} {type_str} → {self.email}"
    
    @property
    def tracking_url(self):
        """URL для просмотра трекинга на фронте."""
        if self.tracking_id:
            frontend_url = getattr(settings, 'FRONTEND_URL', '')
            return f"{frontend_url}/tracking?tid={self.tracking_id}"
        return None
