from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType


class OrderFile(models.Model):
    order = models.ForeignKey("FbiApostilleOrder", on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to='orders/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"File for Order #{self.order_id}"

class FbiApostilleOrder(models.Model):
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    country_name = models.CharField(max_length=100)
    address = models.TextField()

    PACKAGE_CHOICES = [
        ('federal_standard', 'Federal Standard Service - $125'),
        ('expedited_federal', 'Expedited Federal Service - $195'),
        ('state_level', 'State-Level Service - $250'),
    ]
    package = models.CharField(max_length=50, choices=PACKAGE_CHOICES)
    count = models.PositiveIntegerField(help_text="Price of $25 per apostille certificate")

    SHIPPING_CHOICES = [
        ('ups_ground', 'UPS Ground (Standard Shipping) - $0.00'),
        ('1day_express', '1-Day Domestic Express (Overnight) - $29.00'),
        ('2day_express', '2-Day Domestic Express - $19.00'),
    ]
    shipping_option = models.CharField(max_length=50, choices=SHIPPING_CHOICES)

    total_price = models.DecimalField(max_digits=10, decimal_places=2, help_text="Total Price")

    is_paid = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"FBI Apostille Order #{self.id} by {self.name}"