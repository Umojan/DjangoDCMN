from django.db import models
from django.conf import settings


class ReviewRequest(models.Model):
    """Record of a review request sent to a customer."""
    
    REVIEW_TYPE_CHOICES = [
        ('google', 'Google Review'),
        ('trustpilot', 'TrustPilot'),
    ]
    
    # Customer data
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
    
    # Track reference
    track = models.ForeignKey(
        'orders.Track', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='review_requests'
    )
    tracking_id = models.CharField(max_length=20, blank=True, default='', db_index=True)
    
    # Review data
    review_type = models.CharField(
        max_length=20, 
        choices=REVIEW_TYPE_CHOICES,
        blank=True,
        default='',
        help_text="Determined automatically based on Leads Won"
    )
    leads_won_before = models.IntegerField(default=0, help_text="Leads Won before update")
    leads_won_after = models.IntegerField(default=0, help_text="Leads Won after update")
    
    # Status
    is_sent = models.BooleanField(default=False, help_text="Whether review request was sent")
    
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
        """URL for viewing tracking on frontend."""
        if self.tracking_id:
            frontend_url = getattr(settings, 'FRONTEND_URL', '')
            return f"{frontend_url}/tracking?tid={self.tracking_id}"
        return None
