from rest_framework import serializers
from .models import FbiApostilleOrder, OrderFile

class FbiApostilleOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = FbiApostilleOrder
        fields = '__all__'
        read_only_fields = ('is_paid', 'created_at')

class OrderFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderFile
        fields = ['id', 'file', 'uploaded_at']