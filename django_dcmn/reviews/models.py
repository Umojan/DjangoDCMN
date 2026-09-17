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
    # True when the customer received the star-rating email (new gated flow, Sep 2026).
    # Only these requests are eligible for the reminder and the rating links.
    stars_email_sent = models.BooleanField(default=False)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    trustpilot_invite_sent_at = models.DateTimeField(null=True, blank=True)
    
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

    @property
    def first_name(self) -> str:
        return (self.name or '').strip().split(' ')[0] if self.name else ''


class Review(models.Model):
    """Customer rating collected from the star links in the review email.

    Positive ratings (>= REVIEWS_POSITIVE_THRESHOLD) are routed to a public platform
    (Google / Trustpilot); everything else stays internal and goes to the managers + Zoho.
    """

    ROUTE_GOOGLE = 'google'
    ROUTE_TRUSTPILOT = 'trustpilot'
    ROUTE_INTERNAL = 'internal'
    ROUTE_CHOICES = [
        (ROUTE_GOOGLE, 'Google'),
        (ROUTE_TRUSTPILOT, 'Trustpilot'),
        (ROUTE_INTERNAL, 'Internal (managers)'),
    ]

    request = models.OneToOneField(ReviewRequest, on_delete=models.CASCADE, related_name='review')
    rating = models.PositiveSmallIntegerField(null=True, blank=True, help_text='1-5 stars')
    route = models.CharField(max_length=20, choices=ROUTE_CHOICES, default=ROUTE_INTERNAL)
    rated_at = models.DateTimeField(null=True, blank=True)

    feedback_text = models.TextField(blank=True, default='')
    callback_requested = models.BooleanField(default=False)
    callback_phone = models.CharField(max_length=50, blank=True, default='')
    feedback_at = models.DateTimeField(null=True, blank=True)

    manager_notified = models.BooleanField(default=False)
    notified_with_feedback = models.BooleanField(default=False)
    zoho_note_id = models.CharField(max_length=50, blank=True, default='')
    zoho_task_id = models.CharField(max_length=50, blank=True, default='')
    zoho_error = models.TextField(blank=True, default='')

    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '⭐ Review'
        verbose_name_plural = '⭐ Reviews'
        ordering = ['-created_at']

    def __str__(self):
        stars = '★' * (self.rating or 0) + '☆' * (5 - (self.rating or 0))
        return f"{stars} {self.request.email} ({self.route})"

    @property
    def is_positive(self) -> bool:
        return self.route in (self.ROUTE_GOOGLE, self.ROUTE_TRUSTPILOT)

    @property
    def stars(self) -> str:
        r = self.rating or 0
        return '★' * r + '☆' * (5 - r)
