# serializers.py
from rest_framework import serializers
from .models import FbiApostilleOrder, OrderFile, FbiServicePackage, ShippingOption

class FbiServicePackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = FbiServicePackage
        fields = '__all__'

class ShippingOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShippingOption
        fields = '__all__'

class OrderFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderFile
        fields = ['id', 'file', 'uploaded_at']

class FbiApostilleOrderSerializer(serializers.ModelSerializer):
    package = serializers.SlugRelatedField(
        slug_field='code',
        queryset=FbiServicePackage.objects.all()
    )
    shipping_option = serializers.SlugRelatedField(
        slug_field='code',
        queryset=ShippingOption.objects.all()
    )

    class Meta:
        model = FbiApostilleOrder
        fields = '__all__'
        read_only_fields = ('is_paid', 'created_at', 'total_price')