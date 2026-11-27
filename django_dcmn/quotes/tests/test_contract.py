"""`quote.v3` contract: the fixtures, leniency, hard errors, kill switches (DESIGN §3, §14.1).

The helpers at the top are shared by the other WP3 test modules (they are not test cases).
"""

import copy
import itertools
import json
import re
import uuid
from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.db import transaction
from django.test import override_settings

from crm.testing import NoNetworkTestCase
from quotes import contract
from quotes.models import QuoteLine, QuoteRejection, QuoteSubmission
from quotes.validation import normalise

URL = '/api/quote-requests/'
CONFIG_URL = '/api/quote-requests/config/'
REFERENCE_RE = re.compile(r'^Q-[1-9]\d{5}$')
RECEIVED_AT_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
TODAY = date(2026, 11, 26)

enabled = override_settings(QUOTE_V3_ENABLED=True, QUOTE_V3_TEST_ONLY=False)
_ips = itertools.count(1)


def next_ip() -> str:
    """A fresh client IP per request, so the IP throttles of one request never affect another."""
    number = next(_ips)
    return f'10.{(number >> 16) & 255}.{(number >> 8) & 255}.{number & 255}'


def load_fixture(name: str) -> dict:
    return json.loads((contract.FIXTURES_DIR / f'{name}.json').read_text(encoding='utf-8'))


def fixture_names() -> list[str]:
    return sorted(path.stem for path in contract.FIXTURES_DIR.glob('*.json'))


def make_body(*, contact: dict | None = None, **top) -> dict:
    """The v5 `success` body with a fresh submission_id; `contact` and top-level keys override it."""
    body = copy.deepcopy(load_fixture('v5_success')['body'])
    body['submission_id'] = str(uuid.uuid4())
    body['contact'].update(contact or {})
    body.update(top)
    return body


def post(client, body, *, ip: str | None = None, headers: dict | None = None, raw: bytes | None = None):
    return client.post(
        URL, data=raw if raw is not None else json.dumps(body), content_type='application/json',
        headers=headers or {}, REMOTE_ADDR=ip or next_ip(),
    )


def normalised(body: dict):
    return normalise(body, today=TODAY)


def row_counts() -> dict:
    return {'submissions': QuoteSubmission.objects.count(), 'lines': QuoteLine.objects.count(),
            'rejections': QuoteRejection.objects.count()}


# --- Tests -------------------------------------------------------------------------------------------

@enabled
class FixtureTests(NoNetworkTestCase):
    def assert_expect(self, name: str) -> None:
        fixture = load_fixture(name)
        expect = fixture['expect']
        response = post(self.client, fixture['body'], headers=fixture.get('headers'))
        self.assertEqual(response.status_code, expect['status'], response.content)
        data = response.json()
        for key, value in expect.get('body', {}).items():
            self.assertEqual(data.get(key), value, f'body.{key}')
        for key in expect.get('body_keys', []):
            self.assertIn(key, data)
        self.assertEqual(row_counts(), expect['rows'])
        if 'submission' in expect:
            quote = QuoteSubmission.objects.get()
            for key, value in expect['submission'].items():
                self.assertEqual(getattr(quote, key), value, f'submission.{key}')
        for position, subset in enumerate(expect.get('lines', []), start=1):
            line = QuoteLine.objects.get(position=position)
            for key, value in subset.items():
                self.assertEqual(getattr(line, key), value, f'lines[{position}].{key}')
        if 'rejection' in expect:
            rejection = QuoteRejection.objects.get()
            for key, value in expect['rejection'].items():
                self.assertEqual(getattr(rejection, key), value, f'rejection.{key}')

    def test_each_fixture_matches_expect_block(self):
        names = fixture_names()
        self.assertGreaterEqual(len(names), 22)
        for name in names:
            with self.subTest(fixture=name):
                with transaction.atomic():
                    self.assert_expect(name)
                    transaction.set_rollback(True)
        self.assertNoHttp()

    def test_v5_success_returns_201_reference_and_rows(self):
        response = post(self.client, load_fixture('v5_success')['body'])

        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(set(data), {'ok', 'reference', 'confirmation_email', 'received_at'})
        self.assertRegex(data['reference'], REFERENCE_RE)
        self.assertRegex(data['received_at'], RECEIVED_AT_RE)
        quote = QuoteSubmission.objects.get(reference=data['reference'])
        lines = list(quote.lines.order_by('position'))
        self.assertEqual([line.service_key for line in lines], ['apostille', 'translation'])
        primary = [line for line in lines if line.is_primary]
        self.assertEqual([line.service_key for line in primary], ['apostille'])
        self.assertEqual(primary[0].price_flag, 'approved')
        self.assertEqual(quote.primary_service, 'apostille')
        self.assertEqual(lines[0].summary_server,
                         'Apostille — FBI background check → Spain · FBI PDF: yes · 1 document')
        self.assertNotIn('turnstile_token', quote.raw_payload)
        self.assertEqual(quote.attribution_data['source'], 'google')
        self.assertNotIn('campaign', quote.attribution_data)   # nulls removed by clean_attribution_data


@enabled
class LeniencyTests(NoNetworkTestCase):
    def test_unknown_answer_key_dropped_not_400(self):
        body = load_fixture('unknown_answer_key')['body']
        response = post(self.client, body)

        self.assertEqual(response.status_code, 201)
        line = QuoteLine.objects.get(position=1)
        self.assertEqual(line.dropped_answers, {'speed': 'unknown_key'})
        self.assertNotIn('speed', line.answers)

    def test_bad_enum_dropped(self):
        body = make_body()
        body['services'][0]['answers'].update({'doc': 'passport', 'count': 150})
        response = post(self.client, body)

        self.assertEqual(response.status_code, 201)
        line = QuoteLine.objects.get(service_key='apostille')
        self.assertEqual(line.dropped_answers, {'doc': 'invalid_enum', 'count': 'invalid_count',
                                                'fbi_pdf': 'hidden'})
        self.assertEqual(line.answers, {'country': 'ES'})
        self.assertEqual(line.answers_raw['doc'], 'passport')

    def test_same_language_needs_review(self):
        response = post(self.client, load_fixture('same_language')['body'])

        self.assertEqual(response.status_code, 201)
        line = QuoteLine.objects.get(service_key='translation')
        self.assertTrue(line.needs_review)
        self.assertEqual(line.review_reasons, ['same_language'])
        self.assertEqual((line.answers['from'], line.answers['to']), ('english', 'english'))
        self.assertTrue(QuoteSubmission.objects.get().needs_review)

    def test_contract_alias_accepted(self):
        body = make_body(services=[{
            'service': 'mobile-notary', 'position': 1, 'source': 'user',
            'answers': {'doc': 'contract', 'zip': '20001', 'when': 'today'},
        }])
        response = post(self.client, body)

        self.assertEqual(response.status_code, 201)
        line = QuoteLine.objects.get()
        self.assertEqual(line.answers, {'doc': 'contract', 'zip': '20001', 'when': 'today'})
        self.assertEqual(line.dropped_answers, {})

    def test_when_label_mapped(self):
        for label, value in (('As soon as possible', 'asap'), ('No rush', 'standard'),
                             ('By a date', 'by_date'), ('Not sure', 'unsure'), ('tomorrow-ish', '')):
            with self.subTest(label=label):
                nq = normalised(make_body(contact={'when': label}))
                self.assertEqual(nq.core['when'], value)
                self.assertEqual('contact.when:dropped' in nq.warnings, value == '')


@enabled
class HardErrorTests(NoNetworkTestCase):
    def test_bad_email_400_with_v5_detail_copy(self):
        response = post(self.client, make_body(contact={'email': 'maria(at)example.com'}))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'ok': False, 'detail': 'Enter a valid email, like name@example.com.',
                                           'errors': {'contact.email': ['invalid']}})
        self.assertEqual(row_counts(), {'submissions': 0, 'lines': 0, 'rejections': 0})

    def test_unknown_service_400_path(self):
        body = make_body()
        body['services'][1]['service'] = 'notary-public'
        response = post(self.client, body)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['errors'], {'services[1].service': ['invalid']})
        self.assertFalse(QuoteSubmission.objects.exists())

    def test_schema_version_400(self):
        response = post(self.client, make_body(schema_version='quote.v2'))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['errors'], {'schema_version': ['unsupported']})

    def test_body_over_64kb_413(self):
        body = make_body()
        body['contact']['message'] = 'x' * (contract.MAX_BODY_BYTES + 1)
        response = post(self.client, body)

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {'ok': False, 'detail': 'too_large'})
        self.assertEqual(row_counts(), {'submissions': 0, 'lines': 0, 'rejections': 0})

    def test_idempotency_header_mismatch_400(self):
        body = make_body()
        response = post(self.client, body, headers={'Idempotency-Key': str(uuid.uuid4())})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['errors'], {'submission_id': ['mismatch']})
        self.assertFalse(QuoteSubmission.objects.exists())

        response = post(self.client, body, headers={'Idempotency-Key': body['submission_id']})
        self.assertEqual(response.status_code, 201)


@enabled
class FilesTests(NoNetworkTestCase):
    def test_nonempty_files_dropped_with_flag(self):
        response = post(self.client, make_body(files=[{'name': 'passport.pdf', 'size': 12345}]))

        self.assertEqual(response.status_code, 201)
        quote = QuoteSubmission.objects.get()
        self.assertTrue(quote.files_dropped)
        self.assertIn('files:dropped', quote.contract_warnings)


class DisabledTests(NoNetworkTestCase):
    def test_disabled_returns_503_with_fallback(self):
        response = post(self.client, make_body())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'ok': False, 'detail': 'unavailable', 'fallback': contract.FALLBACK})
        self.assertEqual(row_counts(), {'submissions': 0, 'lines': 0, 'rejections': 0})

        response = self.client.get(CONFIG_URL)
        self.assertEqual(response.status_code, 503)
        self.assertEqual((response.json()['enabled'], response.json()['fallback']), (False, contract.FALLBACK))

        with enabled:
            response = self.client.get(CONFIG_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'public, max-age=60')
        self.assertEqual(response.json(), {
            'ok': True, 'enabled': True, 'test_only': False, 'schema_version': 'quote.v3',
            'turnstile_required': False,
            'uploads': {'enabled': False, 'max_files': 5, 'max_mb': 10,
                        'types': ['application/pdf', 'image/jpeg', 'image/png', 'image/heic']},
            'fallback': contract.FALLBACK,
        })

    @override_settings(QUOTE_V3_ENABLED=True, QUOTE_V3_TEST_ONLY=True)
    def test_test_only_rejects_non_zz_test(self):
        response = post(self.client, make_body())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail'], 'unavailable')
        self.assertFalse(QuoteSubmission.objects.exists())

        response = post(self.client, make_body(contact={'full_name': 'ZZ TEST Maria'}))
        self.assertEqual(response.status_code, 201)
        self.assertTrue(QuoteSubmission.objects.get().is_test)


@enabled
class NoExternalCallTests(NoNetworkTestCase):
    def test_post_makes_no_http_and_publishes_on_commit(self):
        with patch('quotes.tasks.process_quote_submission.delay') as process, \
                patch('quotes.tasks.send_quote_staff_email.apply_async') as staff, \
                patch('quotes.tasks.send_quote_confirmation_email.delay') as confirmation:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                response = post(self.client, make_body())
                self.assertEqual(response.status_code, 201)
                process.assert_not_called()
                staff.assert_not_called()

            self.assertEqual(len(callbacks), 1)
            callbacks[0]()

        pk = QuoteSubmission.objects.get().pk
        process.assert_called_once_with(pk)
        staff.assert_called_once_with((pk,), countdown=20)
        confirmation.assert_not_called()   # QUOTE_V3_CONFIRMATION_EMAIL is off
        self.assertNoHttp()


class SchemaEnumTests(NoNetworkTestCase):
    """quotes/contract.py and quote.v3.schema.json list the same enums (DESIGN §18 #3)."""

    def test_schema_enums_match_contract(self):
        schema = json.loads(contract.SCHEMA_PATH.read_text(encoding='utf-8'))
        defs = schema['$defs']
        line, contact = defs['serviceLine']['properties'], defs['contact']['properties']
        self.assertEqual(schema['properties']['schema_version']['const'], contract.SCHEMA_VERSION)
        self.assertEqual(tuple(defs['serviceKey']['enum']), contract.SERVICE_KEYS)
        self.assertEqual(schema['properties']['services']['maxItems'], contract.MAX_SERVICES)
        self.assertEqual(tuple(line['source']['enum']), contract.LINE_SOURCES)
        self.assertEqual(_values(line['addon_of']), set(contract.ADDON_PARENTS))
        self.assertEqual(_values(line['answer_source']['additionalProperties']), set(contract.ANSWER_SOURCES))
        self.assertEqual(_values(defs['priceShown']['properties']['kind']), set(contract.PRICE_KINDS))
        self.assertEqual(_values(defs['priceShown']['properties']['tier']), set(contract.PRICE_TIERS))
        self.assertEqual(_values(contact['preferred_channel']), set(contract.CHANNELS))
        self.assertEqual(_values(contact['when']), set(contract.WHEN_VALUES))

        answers_by_service = {
            rule['if']['properties']['service']['const']: rule['then']['properties']['answers']
            for rule in defs['serviceLine']['allOf']
        }
        self.assertEqual(set(answers_by_service), set(contract.SERVICE_KEYS))
        for service, answers in contract.ANSWERS.items():
            spec = answers_by_service[service].get('properties', {})
            with self.subTest(service=service):
                self.assertEqual(set(spec), set(answers))
                for key, answer in answers.items():
                    if answer.kind == contract.ENUM:
                        self.assertEqual(_values(spec[key]), set(answer.values), f'{service}.{key}')


def _values(node: dict) -> set:
    return {value for value in node['enum'] if value is not None}


@enabled
class AdminTests(NoNetworkTestCase):
    """The read-only admin renders; `raw_payload` is shown to superusers only (DESIGN §12.3)."""

    def test_admin_pages_render_read_only(self):
        reference = post(self.client, make_body()).json()['reference']
        quote = QuoteSubmission.objects.get(reference=reference)
        admin = User.objects.create_superuser('admin', 'admin@example.com', 'pw')
        staff = User.objects.create_user('staff', 'staff@example.com', 'pw', is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename='view_quotesubmission'))

        self.client.force_login(admin)
        listing = self.client.get('/admin/quotes/quotesubmission/')
        self.assertContains(listing, reference)
        page = self.client.get(f'/admin/quotes/quotesubmission/{quote.pk}/change/')
        self.assertContains(page, 'Raw payload (superuser only)')
        self.assertContains(page, f'[quote.v3] {reference}')
        self.assertNotContains(page, 'name="_save"')
        for path in ('quoterejection', 'quotephonematch', 'shadowrun'):
            with self.subTest(model=path):
                self.assertEqual(self.client.get(f'/admin/quotes/{path}/').status_code, 200)

        self.client.force_login(staff)
        page = self.client.get(f'/admin/quotes/quotesubmission/{quote.pk}/change/')
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'Raw payload (superuser only)')
