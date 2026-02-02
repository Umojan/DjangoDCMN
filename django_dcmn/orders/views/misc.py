# orders/views/misc.py
"""Miscellaneous views (test endpoints, callbacks, etc.)"""

from django.http import HttpResponse, JsonResponse
from django.core.mail import send_mail


def test_email(request):
    """Test email sending via Resend."""
    try:
        send_mail(
            subject="🚀 Django Email Test via Resend",
            message="If you see this, Resend is working perfectly!",
            from_email="support@dcmobilenotary.net",
            recipient_list=["support@dcmobilenotary.com"],
            fail_silently=False,
        )
        return JsonResponse({"status": "✅ Email sent!"})
    except Exception as e:
        return JsonResponse({"status": "❌ Failed", "error": str(e)}, status=500)


def zoho_callback(request):
    """Zoho OAuth callback handler."""
    code = request.GET.get('code')
    if code:
        return HttpResponse(f'Authorization code: {code}')
    return HttpResponse('No code found', status=400)
