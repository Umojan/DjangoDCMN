# django_dcmn/views.py
from django.conf import settings
from django.http import FileResponse, Http404
import os

def serve_media_file(request, path):
    file_path = os.path.join(settings.MEDIA_ROOT, path)

    if os.path.exists(file_path):
        return FileResponse(open(file_path, 'rb'))
    raise Http404("File not found")