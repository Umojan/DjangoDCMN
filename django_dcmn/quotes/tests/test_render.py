"""Staff and Zoho texts (DESIGN §5.3–5.5): Client_Comments, the receipt, Quote_Lines_Text, prices."""

import re
from datetime import UTC, datetime

from crm.models import ServiceLink
from crm.testing import NoNetworkTestCase
from quotes import contract
from quotes.intake import RequestMeta, attribution_from_body, save_submission
from quotes.render import (
    LINES_MORE_IN_NOTES, MORE_IN_NOTES, comments_text, lines_text, price_text, receipt_text,
)
from quotes.tests.test_contract import load_fixture, make_body, normalised


def saved(body: dict):
    meta = RequestMeta(ip='10.0.0.1', user_agent='test', turnstile_result='skipped',
                       attribution=attribution_from_body(body))
    return save_submission(normalised(body), meta).quote


class CommentsTests(NoNetworkTestCase):
    def test_v5_success_block_matches_design(self):
        quote = saved(load_fixture('v5_success')['body'])

        self.assertEqual(comments_text(quote), '\n'.join([
            f'[quote.v3] {quote.reference} · 2 services · EN · web form v5-2026-11-26',
            '1. Apostille — FBI background check → Spain · FBI PDF: yes · 1 document',
            '   Price shown: Apostille · Federal · from $125 · + $25 per document',
            '2. Translation (add-on of Apostille) — English → Spanish · Traducción jurada',
            '   Price shown: Price after free review',
            'Contact: WhatsApp (opted in) · When: As soon as possible',
            'From: /schedule-an-appointment?service=apostille&doc=fbi ← /apostille-fbi',
        ]))

    def test_truncation_keeps_header_and_services(self):
        body = load_fixture('max_11_services')['body']
        body['contact']['message'] = ('Please call me after five. ' * 80)[:2000]
        quote = saved(body)

        text = comments_text(quote)

        self.assertLessEqual(len(text), 2000)
        rows = text.splitlines()
        self.assertTrue(rows[0].startswith(f'[quote.v3] {quote.reference} · 11 services · EN'))
        numbered = [row for row in rows if re.match(r'^\d+\. ', row)]
        self.assertEqual(len(numbered), 11)
        for number, (row, key) in enumerate(zip(numbered, contract.SERVICE_KEYS), start=1):
            self.assertTrue(row.startswith(f'{number}. {contract.SERVICE_DISPLAY[key]}'), row)
        message = next(row for row in rows if row.startswith('Message: '))
        self.assertTrue(message.endswith('…'))
        self.assertLess(len(message), 2000)
        self.assertEqual(rows[-1], MORE_IN_NOTES)
        self.assertIn('Contact: ', text)

        tiny = comments_text(quote, limit=300)
        self.assertLessEqual(len(tiny), 300)
        self.assertTrue(tiny.startswith('[quote.v3] '))
        self.assertTrue(tiny.endswith(MORE_IN_NOTES))


class ReceiptTests(NoNetworkTestCase):
    def test_contains_all_answers_and_dropped(self):
        quote = saved(load_fixture('stale_keys')['body'])

        text = receipt_text(quote)

        for fragment in (
            'doc=fbi_report', 'country=ES', 'kind=personal', 'country=AE',
            'doc_kind="vital" (unknown_key)', 'doc_count="2" (unknown_key)', 'issuer_state="MD" (unknown_key)',
            'state="VA" (hidden)', 'kindFrom="personal" (unknown_key)',
            'docname="FBI background check" (unknown_key)', 'fromdoc="fbi_report" (unknown_key)',
            'Contract warnings: combo:ignored', f'Name: {quote.full_name}', f'Email: {quote.email}',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)
        self.assertLessEqual(len(text), 10000)

        success = receipt_text(saved(load_fixture('v5_success')['body']))
        for fragment in ('Phone: +12025550147', 'Attribution: google / organic · first visit 25 Nov · 5 pages',
                         'Entry: url_param · service=apostille · doc=fbi · preselected apostille',
                         'Consent: terms statement terms-2026-11 · consent-v5-2026-11-26 · WhatsApp opt-in yes'):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, success)

        cut = receipt_text(quote, limit=200)
        self.assertLessEqual(len(cut), 200)
        self.assertTrue(cut.endswith(MORE_IN_NOTES))


class LinesTextTests(NoNetworkTestCase):
    def test_statuses_in_words(self):
        quote = saved(load_fixture('v5_success')['body'])
        self.assertEqual(lines_text(quote), '\n'.join([
            '1. Apostille — FBI background check → Spain · FBI PDF: yes · 1 document · from $125 + $25/doc — REQUESTED',
            '2. Translation (add-on of Apostille) — English → Spanish · Traducción jurada · price after free review'
            ' — REQUESTED',
        ]))

        quote = saved(load_fixture('max_11_services')['body'])
        done = datetime(2026, 11, 26, 16, 0, tzinfo=UTC)
        links = {
            'apostille': ServiceLink(state='created', target_module='Apostille_Services', trigger='intake',
                                     done_at=done),
            'embassy': ServiceLink(state='adopted', target_module='Embassy_Legalization'),
            'translation': ServiceLink(state='pending'),
            'fbi': ServiceLink(state='needs_decision'),
            'fingerprinting': ServiceLink(state='failed'),
            'mobile-notary': ServiceLink(state='degraded'),
            'ron': ServiceLink(state='dismissed'),
            'office-notary': ServiceLink(state='suggested'),
            'i9': ServiceLink(state='target_deleted'),
        }

        rows = dict(zip(quote.service_keys, lines_text(quote, links).splitlines()))

        expected = {
            'apostille': 'CREATED → Apostille Services (26 Nov, intake)',
            'embassy': 'LINKED → Embassy Legalization',
            'translation': 'CREATING',
            'fbi': 'NEEDS DECISION',
            'retrieval': 'NO PIPELINE (handle from the quote)',
            'fingerprinting': 'FAILED',
            'mobile-notary': 'SETUP CHANGED',
            'ron': 'DISMISSED',
            'office-notary': 'SUGGESTED',
            'i9': 'REQUESTED (Zoho record deleted)',
            'other': 'NO PIPELINE (handle from the quote)',
        }
        for key, status in expected.items():
            with self.subTest(service=key):
                self.assertTrue(rows[key].endswith(f' — {status}'), rows[key])
        self.assertLessEqual(len(lines_text(quote, links)), 2000)

        short = lines_text(quote, links, limit=600)
        self.assertLessEqual(len(short), 600)
        self.assertTrue(short.endswith(LINES_MORE_IN_NOTES))
        self.assertIn(' — CREATED → Apostille Services (26 Nov, intake)', short)

    def test_implied_service_is_suggested(self):
        body = make_body(services=[{'service': 'fbi', 'position': 1, 'source': 'user',
                                    'answers': {'apostille': 'yes', 'where': 'dc', 'people': 2}}])
        quote = saved(body)

        self.assertEqual(lines_text(quote).splitlines(), [
            '1. FBI Background Check — DC office · apostille needed: yes · 2 people — REQUESTED',
            'Suggested: Apostille (client said the FBI report needs an apostille)',
        ])
        self.assertIn('Suggested: Apostille (client said the FBI report needs an apostille)', comments_text(quote))
        created = {'apostille': ServiceLink(state='created', target_module='Apostille_Services', trigger='button',
                                            done_at=datetime(2026, 11, 27, 15, 0, tzinfo=UTC))}
        self.assertTrue(lines_text(quote, created).endswith(' — CREATED → Apostille Services (27 Nov, button)'))


class PriceTests(NoNetworkTestCase):
    def test_server_price_text_rendered_client_text_marked_unapproved(self):
        self.assertEqual(price_text('apostille', 'federal', 'approved'),
                         'Apostille · Federal · from $125 · + $25 per document')
        self.assertEqual(price_text('apostille', 'dc', 'approved'), 'Apostille · DC · from $195 · + $25 per document')
        self.assertEqual(price_text('embassy', None, 'approved'),
                         'Embassy legalization · from $495 · + $50 per document + embassy fee')
        self.assertEqual(price_text('translation', None, 'review'), 'Price after free review')
        self.assertEqual(price_text('apostille', 'federal', 'unapproved'), 'Price after free review')
        self.assertEqual(price_text('apostille', 'federal', 'missing'), '')

        body = make_body()
        body['services'][0]['price_shown'] = {'kind': 'from', 'amount_usd': 99, 'tier': 'federal',
                                              'per_document_usd': 25, 'plus_embassy_fee': False,
                                              'text': 'Apostille · Federal · from $99 · + $25 per document'}
        quote = saved(body)
        self.assertEqual(quote.lines.get(position=1).price_flag, 'unapproved')

        comments = comments_text(quote).splitlines()
        self.assertEqual(comments[2:4], [
            '   Price shown: Price after free review',
            '   client saw: "Apostille · Federal · from $99 · + $25 per document" (unapproved)',
        ])
        self.assertNotIn('Price shown: Apostille · Federal · from $99', '\n'.join(comments))
        receipt = receipt_text(quote)
        self.assertIn('client saw: "Apostille · Federal · from $99 · + $25 per document" (unapproved)', receipt)
        self.assertIn(f'Check: unapproved price on line 1 (price table {contract.PRICE_TABLE_VERSION})', receipt)
        self.assertIn(' · price after free review — REQUESTED', lines_text(quote))

        approved = saved(make_body(contact={'email': 'other@example.com'}))
        self.assertNotIn('client saw', comments_text(approved))


class ContactTests(NoNetworkTestCase):
    def test_no_opt_in_channel_explained_in_comments(self):
        body = make_body(consent={'terms_method': 'statement', 'terms_version': 't', 'whatsapp_opt_in': False,
                                  'sms_opt_in': False, 'consent_text_version': 'c'})
        quote = saved(body)

        self.assertEqual((quote.preferred_channel, quote.effective_channel), ('whatsapp', ''))
        self.assertIn('Contact: asked for WhatsApp, no opt-in — use call or email · When: As soon as possible',
                      comments_text(quote).splitlines())

        cases = (
            ({'preferred_channel': 'text'}, {'sms_opt_in': True},
             'Contact: Text (opted in) · When: As soon as possible'),
            ({'preferred_channel': 'call', 'when': None}, {}, 'Contact: Call'),
            ({'preferred_channel': None, 'when': 'standard'}, {}, 'Contact: no preference · When: No rush'),
        )
        for number, (contact, consent, line) in enumerate(cases):
            with self.subTest(line=line):
                body = make_body(contact={'email': f'c{number}@example.com', **contact})
                body['consent'] = {**body['consent'], 'whatsapp_opt_in': False, **consent}
                self.assertIn(line, comments_text(saved(body)).splitlines())
