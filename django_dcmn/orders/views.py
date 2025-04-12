from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .serializers import FbiApostilleOrderSerializer, OrderFileSerializer
from .models import OrderFile


class CreateFbiOrderView(APIView):
    def post(self, request, format=None):
        order_serializer = FbiApostilleOrderSerializer(data=request.data)

        if order_serializer.is_valid():
            order = order_serializer.save()

            file_urls = []
            if request.FILES:
                files = request.FILES.getlist('files')
                for f in files:
                    file_instance = OrderFile.objects.create(order=order, file=f)
                    file_url = request.build_absolute_uri(file_instance.file.url)
                    file_urls.append(file_url)

            return Response({
                'message': 'Order created successfully',
                'order_id': order.id,
                'file_urls': file_urls if file_urls else None
            }, status=status.HTTP_201_CREATED)

        return Response(order_serializer.errors, status=status.HTTP_400_BAD_REQUEST)