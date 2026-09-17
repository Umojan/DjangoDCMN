"""Review gating: signed rating links, routing, manager alerts and Zoho note/task.

Flow (Sep 2026):
  Zoho "Send Review" stage → ReviewRequest → email with 5 star links
  → GET  /api/reviews/r/<token>/<stars>/  (interstitial page, auto-POSTs)
  → POST /api/reviews/r/<token>/<stars>/  (records Review, 302):
        rating >= REVIEWS_POSITIVE_THRESHOLD → 302 straight to the Google review form (first order)
                                                or the Trustpilot review form (returning customer)
        rating <  threshold                  → FRONTEND_URL/feedback?t=<token> (internal form)
  → POST /api/reviews/feedback/  → text saved → managers email + Zoho note + Zoho task
"""
from __future__ import annotations

import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.core import signing
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.utils import timezone

from .models import Review, ReviewRequest

logger = logging.getLogger(__name__)

TOKEN_SALT = 'reviews.rate'
TOKEN_MAX_AGE = 60 * 60 * 24 * int(getattr(settings, 'REVIEWS_TOKEN_MAX_AGE_DAYS', 90))

# Zoho module label as sent by the Zoho workflow → API module name
ZOHO_MODULE_BY_LABEL = {
    'FBI': 'Deals',
    'FBI_Background_Checks': 'Deals',
    'FBI Background Checks': 'Deals',
    'Deals': 'Deals',
    'Apostille': 'Apostille_Services',
    'Apostille_Services': 'Apostille_Services',
    'Apostille Services': 'Apostille_Services',
    'Embassy': 'Embassy_Legalization',
    'Embassy Legalization': 'Embassy_Legalization',
    'Embassy_Legalization': 'Embassy_Legalization',
    'Translation': 'Translation_Services',
    'Translation_Services': 'Translation_Services',
    'Translation Services': 'Translation_Services',
    'Triple Seal': 'Triple_Seal_Apostilles',
    'Triple_Seal_Apostilles': 'Triple_Seal_Apostilles',
    'Marriage': 'Triple_Seal_Apostilles',
    'I-9': 'I_9_Verification',
    'I_9_Verification': 'I_9_Verification',
    'Notary': 'Notary_Services',
    'Notary_Services': 'Notary_Services',
    'Fingerprinting': 'FINGERPRINT_SERVICES',
    'FINGERPRINT_SERVICES': 'FINGERPRINT_SERVICES',
}


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def positive_threshold() -> int:
    return int(getattr(settings, 'REVIEWS_POSITIVE_THRESHOLD', 4))


def google_review_url() -> str:
    return getattr(settings, 'GOOGLE_REVIEW_URL', '')


def trustpilot_review_url() -> str:
    return getattr(settings, 'TRUSTPILOT_REVIEW_URL', 'https://www.trustpilot.com/evaluate/dcmobilenotary.com')


def frontend_url() -> str:
    return getattr(settings, 'FRONTEND_URL', 'https://www.dcmobilenotary.com').rstrip('/')


def api_base_url() -> str:
    return getattr(settings, 'BASE_URL', '').rstrip('/')


def feedback_page_url(token: str) -> str:
    return f"{frontend_url()}{getattr(settings, 'REVIEWS_FEEDBACK_PATH', '/feedback')}?t={token}"


def notification_recipients() -> list[str]:
    for key in ('REVIEWS_NOTIFY_EMAILS', 'APPLICATIONS_NOTIFY_EMAILS', 'EMAIL_OFFICE_RECEIVER'):
        recipients = [e.strip() for e in (getattr(settings, key, None) or []) if e and e.strip()]
        if recipients:
            return recipients
    return []


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def make_token(review_request: ReviewRequest) -> str:
    return signing.dumps({'r': review_request.id}, salt=TOKEN_SALT, compress=True)


def parse_token(token: str) -> ReviewRequest | None:
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except signing.BadSignature:
        return None
    try:
        return ReviewRequest.objects.select_related('review').get(id=int(data.get('r')))
    except (ReviewRequest.DoesNotExist, TypeError, ValueError):
        return None


def rate_link(review_request: ReviewRequest, stars: int, token: str | None = None) -> str:
    token = token or make_token(review_request)
    return f"{api_base_url()}/api/reviews/r/{token}/{int(stars)}/"


def star_links(review_request: ReviewRequest) -> list[dict]:
    token = make_token(review_request)
    labels = {1: 'Poor', 2: 'Fair', 3: 'Okay', 4: 'Good', 5: 'Excellent'}
    return [{'stars': n, 'label': labels[n], 'url': rate_link(review_request, n, token)} for n in range(1, 6)]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route_for(review_request: ReviewRequest, rating: int) -> str:
    if rating >= positive_threshold():
        if review_request.review_type == 'trustpilot':
            return Review.ROUTE_TRUSTPILOT
        return Review.ROUTE_GOOGLE
    return Review.ROUTE_INTERNAL


def redirect_target(review: Review, token: str) -> str:
    if review.route == Review.ROUTE_GOOGLE:
        return google_review_url()
    if review.route == Review.ROUTE_TRUSTPILOT:
        # Straight to the Trustpilot review form; the verified AFS invite email goes out in the background.
        return trustpilot_review_url()
    return feedback_page_url(token)


def record_rating(review_request: ReviewRequest, rating: int, *, ip: str | None = None,
                  user_agent: str = '') -> tuple[Review, bool]:
    """Store the first rating for a request. Later clicks never overwrite it.

    Returns (review, created).
    """
    rating = max(1, min(5, int(rating)))
    review = getattr(review_request, 'review', None)
    if review is not None and review.rating:
        return review, False

    if review is None:
        review = Review(request=review_request)
    review.rating = rating
    review.route = route_for(review_request, rating)
    review.rated_at = timezone.now()
    review.ip = ip
    review.user_agent = (user_agent or '')[:300]
    review.save()
    return review, True


def public_context(review_request: ReviewRequest) -> dict:
    review = getattr(review_request, 'review', None)
    return {
        'name': review_request.first_name,
        'service_label': service_label(review_request.zoho_module),
        'tracking_id': review_request.tracking_id,
        'rating': review.rating if review else None,
        'route': review.route if review else None,
        'feedback_submitted': bool(review and review.feedback_at),
        'google_url': google_review_url(),
        'trustpilot_url': trustpilot_review_url(),
        'positive_threshold': positive_threshold(),
    }


def service_label(module: str) -> str:
    from .tasks import _get_service_label
    return _get_service_label(module)


# ---------------------------------------------------------------------------
# Manager alert
# ---------------------------------------------------------------------------

def admin_url(review: Review) -> str:
    return f"{api_base_url()}/admin/reviews/review/{review.id}/change/"


def zoho_record_url(review_request: ReviewRequest) -> str:
    module = ZOHO_MODULE_BY_LABEL.get(review_request.zoho_module or '', '')
    if module and review_request.zoho_deal_id:
        return f"https://crm.zoho.com/crm/tab/{module}/{review_request.zoho_deal_id}"
    if review_request.zoho_contact_id:
        return f"https://crm.zoho.com/crm/tab/Contacts/{review_request.zoho_contact_id}"
    return ''


def build_alert_subject(review: Review, update: bool = False) -> str:
    rr = review.request
    who = rr.name or rr.email
    tid = f" {rr.tracking_id}" if rr.tracking_id else ''
    prefix = 'Feedback added' if update else 'Negative feedback'
    return f"⚠ {prefix}: {review.rating or '?'}★ — {who}, {service_label(rr.zoho_module)}{tid}"


def send_manager_alert(review: Review, update: bool = False) -> bool:
    recipients = notification_recipients()
    if not recipients:
        logger.warning('Review alert: no recipients configured')
        return False
    rr = review.request
    ctx = {
        'review': review,
        'rr': rr,
        'service_label': service_label(rr.zoho_module),
        'admin_url': admin_url(review),
        'zoho_url': zoho_record_url(rr),
        'tracking_url': rr.tracking_url,
        'update': update,
    }
    html = render_to_string('emails/review_negative_alert.html', ctx)
    msg = EmailMessage(
        subject=build_alert_subject(review, update=update),
        body=html,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
        reply_to=[rr.email] if rr.email else None,
    )
    msg.content_subtype = 'html'
    msg.send(fail_silently=False)
    return True


# ---------------------------------------------------------------------------
# Zoho note + task
# ---------------------------------------------------------------------------

def _zoho_headers() -> dict:
    from orders.zoho_sync import get_access_token
    return {'Authorization': f'Zoho-oauthtoken {get_access_token()}', 'Content-Type': 'application/json'}


def _zoho_parent(review_request: ReviewRequest) -> tuple[str, str] | None:
    """Return (module, record_id) to attach note/task to. Deal record first, contact as fallback."""
    module = ZOHO_MODULE_BY_LABEL.get(review_request.zoho_module or '')
    if module and review_request.zoho_deal_id:
        return module, review_request.zoho_deal_id
    if review_request.zoho_contact_id:
        return 'Contacts', review_request.zoho_contact_id
    return None


def note_content(review: Review) -> str:
    rr = review.request
    lines = [
        f"Rating: {review.rating}/5 {review.stars}",
        f"Customer: {rr.name or '-'} <{rr.email}> {rr.phone or ''}".rstrip(),
    ]
    if rr.tracking_id:
        lines.append(f"Tracking ID: {rr.tracking_id}")
    if review.feedback_text:
        lines += ['', 'Feedback:', review.feedback_text.strip()]
    else:
        lines += ['', '(no written feedback yet)']
    if review.callback_requested:
        lines.append(f"\nCallback requested: {review.callback_phone or rr.phone or 'phone n/a'}")
    lines.append(f"\nAdmin: {admin_url(review)}")
    return '\n'.join(lines)


def create_zoho_note(review: Review) -> str | None:
    from orders.zoho_sync import ZOHO_API_DOMAIN
    parent = _zoho_parent(review.request)
    if not parent:
        return None
    module, record_id = parent
    payload = {'data': [{
        'Note_Title': f'Negative review ({review.rating}★) — internal feedback',
        'Note_Content': note_content(review),
        'Parent_Id': record_id,
        'se_module': module,
    }]}
    resp = requests.post(f'{ZOHO_API_DOMAIN}/crm/v2/Notes', headers=_zoho_headers(), json=payload, timeout=30)
    data = resp.json() if resp.content else {}
    item = (data.get('data') or [{}])[0]
    if resp.status_code in (200, 201) and item.get('code') == 'SUCCESS':
        return item['details']['id']
    raise RuntimeError(f'Zoho note failed ({module}/{record_id}): {resp.status_code} {resp.text[:300]}')


def create_zoho_task(review: Review) -> str | None:
    from orders.zoho_sync import ZOHO_API_DOMAIN
    rr = review.request
    parent = _zoho_parent(rr)
    if not parent:
        return None
    module, record_id = parent
    due = (timezone.now() + timedelta(days=1)).date().isoformat()
    task = {
        'Subject': f'Call back: negative review {review.rating}★ — {rr.name or rr.email}',
        'Status': 'Not Started',
        'Priority': 'High',
        'Due_Date': due,
        'Description': note_content(review),
    }
    if module == 'Contacts':
        task['Who_Id'] = record_id
    else:
        task['What_Id'] = record_id
        task['$se_module'] = module
    owner_id = _record_owner_id(module, record_id)
    if owner_id:
        task['Owner'] = owner_id
    resp = requests.post(f'{ZOHO_API_DOMAIN}/crm/v2/Tasks', headers=_zoho_headers(), json={'data': [task]}, timeout=30)
    data = resp.json() if resp.content else {}
    item = (data.get('data') or [{}])[0]
    if resp.status_code in (200, 201) and item.get('code') == 'SUCCESS':
        return item['details']['id']
    raise RuntimeError(f'Zoho task failed ({module}/{record_id}): {resp.status_code} {resp.text[:300]}')


def _record_owner_id(module: str, record_id: str) -> str | None:
    from orders.zoho_sync import ZOHO_API_DOMAIN
    try:
        resp = requests.get(f'{ZOHO_API_DOMAIN}/crm/v2/{module}/{record_id}', headers=_zoho_headers(),
                            params={'fields': 'Owner'}, timeout=30)
        if resp.status_code == 200:
            owner = (resp.json().get('data') or [{}])[0].get('Owner') or {}
            return owner.get('id')
    except Exception as exc:  # noqa: BLE001
        logger.warning('Review: could not read record owner %s/%s: %s', module, record_id, exc)
    return None


def push_negative_to_zoho(review: Review) -> None:
    """Create note (always, once per text state) and task (once). Errors stored, never raised."""
    errors = []
    try:
        note_id = create_zoho_note(review)
        if note_id:
            review.zoho_note_id = note_id
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
    if not review.zoho_task_id:
        try:
            task_id = create_zoho_task(review)
            if task_id:
                review.zoho_task_id = task_id
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
    review.zoho_error = '\n'.join(errors)
    review.save(update_fields=['zoho_note_id', 'zoho_task_id', 'zoho_error', 'updated_at'])
    if errors:
        logger.warning('Review %s Zoho errors: %s', review.id, errors)


def notify_negative(review: Review) -> str:
    """Send the manager alert + Zoho note/task. Idempotent.

    Returns 'sent', 'update' or 'skipped'.
    """
    review.refresh_from_db()
    has_text = bool(review.feedback_text.strip())
    if not review.manager_notified:
        send_manager_alert(review, update=False)
        review.manager_notified = True
        review.notified_with_feedback = has_text
        review.save(update_fields=['manager_notified', 'notified_with_feedback', 'updated_at'])
        push_negative_to_zoho(review)
        return 'sent'
    if has_text and not review.notified_with_feedback:
        send_manager_alert(review, update=True)
        review.notified_with_feedback = True
        review.save(update_fields=['notified_with_feedback', 'updated_at'])
        push_negative_to_zoho(review)
        return 'update'
    return 'skipped'


# ---------------------------------------------------------------------------
# Trustpilot AFS invite (returning customers, positive rating only)
# ---------------------------------------------------------------------------

def send_trustpilot_invite(review_request: ReviewRequest) -> bool:
    if review_request.trustpilot_invite_sent_at:
        return False
    trigger = getattr(settings, 'TRUSTPILOT_TRIGGER_EMAIL', '')
    html = render_to_string('emails/review_trustpilot_invite.html', {
        'name': review_request.first_name or 'there',
        'service_label': service_label(review_request.zoho_module),
        'tid': review_request.tracking_id,
        'trustpilot_url': trustpilot_review_url(),
    })
    msg = EmailMessage(
        subject='Thank you for your rating — DC Mobile Notary',
        body=html,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[review_request.email],
        bcc=[trigger] if trigger else None,
    )
    msg.content_subtype = 'html'
    msg.send(fail_silently=False)
    review_request.trustpilot_invite_sent_at = timezone.now()
    review_request.save(update_fields=['trustpilot_invite_sent_at'])
    return True
