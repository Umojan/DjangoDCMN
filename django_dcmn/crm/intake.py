"""Auto-intake of a v5 quote's primary line (DESIGN §7.3), called by the quotes pipeline (step `intake`).

Idempotent: the ServiceLink of (qr:{pk}, service, 1) is unique and the Notes are found by title prefix.
Everything goes through `crm.engine.handle`, so the engine gates (mode, test-only, sources) apply.
"""

import logging

from quotes import render
from quotes.models import GAQL_MODULE, QuoteSubmission

from . import flags, rules, sources, zoho
from .engine import handle
from .models import ServiceSignal
from .registry import REGISTRY

logger = logging.getLogger(__name__)

SYSTEM_ACTOR = {'kind': 'system'}
SPAM_NOTE_EVENT = 'possible spam'
IMPLIED_KIND = 'apostille_implied'


def note_title(quote: QuoteSubmission, event: str) -> str:
    return f'DCMN · qr:{quote.pk} · {event}'


def start_intake(quote_id: int) -> list[int]:
    """ServiceLink ids queued for the quote's primary line; [] when nothing is created automatically."""
    quote = QuoteSubmission.objects.get(pk=quote_id)
    suggest_implied(quote)
    if quote.intake_mode != 'primary':
        return []
    if quote.spam_status != 'clean':
        _spam_note(quote)
        return []
    primary = quote.lines.filter(is_primary=True).first() or quote.lines.order_by('position').first()
    spec = REGISTRY.get(primary.service_key) if primary else None
    if spec is None or not spec.intake:
        return []
    result = handle(sources.for_quote(quote), [primary.service_key], mode='create', trigger='intake',
                    caller='system', actor=SYSTEM_ACTOR, client_request_id=f'qr:{quote.pk}:intake')
    return [r['link_id'] for r in result.body.get('results') or [] if r.get('link_id')]


def _spam_note(quote: QuoteSubmission) -> None:
    """The GAQL Note of §7.3 step 2, once."""
    if not quote.zoho_record_id:
        return
    title = note_title(quote, SPAM_NOTE_EVENT)
    notes = zoho.list_notes(GAQL_MODULE, quote.zoho_record_id)
    if any(str(n.get('Note_Title') or '').startswith(title) for n in notes):
        return
    reasons = ', '.join(render.SPAM_SIGNALS.get(code, code) for code in quote.spam_reasons or []) or 'unclear signals'
    zoho.add_note(GAQL_MODULE, quote.zoho_record_id, title,
                  f'Possible spam ({reasons}) — nothing was created automatically. '
                  'If the request is real, use Create … or Add service…')
    logger.info('intake_spam_note quote=%s', quote.pk)


def suggest_implied(quote: QuoteSubmission) -> list[ServiceSignal]:
    """§7.3 step 6: one `apostille_implied` signal per implied service, evaluated by the rules (suggest by default).

    Runs for suspect quotes too (a suggestion creates no record). Signals wait while the engine is off.
    """
    signals = []
    lines = list(quote.lines.order_by('position'))
    requested = {line.service_key for line in lines}
    for line in lines:
        for service in line.implied_services or []:
            if service not in REGISTRY or service in requested:
                continue
            signal, _ = ServiceSignal.objects.get_or_create(
                dedupe_key=f'{IMPLIED_KIND}:qr:{quote.pk}:{service}',
                defaults={'kind': IMPLIED_KIND, 'source_kind': 'quote', 'source_key': f'qr:{quote.pk}',
                          'source_module': GAQL_MODULE, 'source_record_id': quote.zoho_record_id, 'quote': quote,
                          'service_key': service, 'data': {'from_line': line.service_key}},
            )
            signals.append(signal)
    if flags.engine_mode() == 'off':
        return signals
    for signal in signals:
        if signal.state == 'new':
            rules.evaluate(signal)
    return signals

