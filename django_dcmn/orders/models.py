# orders/models.py
from django.db import models

class FbiServicePackage(models.Model):
    code = models.CharField(max_length=50, unique=True)
    label = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=7, decimal_places=2)

    def __str__(self):
        return self.label

    class Meta:
        verbose_name = 'FBI Apostille — Service Package'
        verbose_name_plural = 'FBI Apostille — Service Packages'


class ShippingOption(models.Model):
    code = models.CharField(max_length=50, unique=True)
    label = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=7, decimal_places=2)

    def __str__(self):
        return self.label

class FbiPricingSettings(models.Model):
    price_per_certificate = models.DecimalField(max_digits=6, decimal_places=2, default=25.00)

    def __str__(self):
        return f"Settings (Per Certificate: {self.price_per_certificate})"

    class Meta:
        verbose_name = 'FBI Apostille — Pricing Setting'

class FbiApostilleOrder(models.Model):
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    country_name = models.CharField(max_length=100)
    address = models.TextField()
    comments = models.TextField(blank=True, null=True)

    package = models.ForeignKey(FbiServicePackage, on_delete=models.CASCADE)
    count = models.PositiveIntegerField(help_text="Price per apostille certificate")
    shipping_option = models.ForeignKey(ShippingOption, on_delete=models.CASCADE)

    total_price = models.DecimalField(max_digits=10, decimal_places=2, help_text="Total Price")
    is_paid = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"FBI Apostille Order #{self.id} by {self.name}"

    class Meta:
        verbose_name = 'FBI Apostille — Order'
        verbose_name_plural = 'FBI Apostille — Orders'

class OrderFile(models.Model):
    order = models.ForeignKey(FbiApostilleOrder, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to='orders/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"File for Order #{self.order.id}"