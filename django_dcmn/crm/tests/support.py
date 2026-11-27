"""Shared fixtures of the engine tests (WP5): the flag set, seeded Zoho metadata, saved v5 quotes and `FakeCrm`,
an in-memory Zoho records API answered through the NoNetworkTestCase spy. Nothing here reaches a network.
"""

import copy
import itertools
import json
import re
import uuid
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from crm import schema
from crm.engine import external_key
from crm.models import ServiceLink
from crm.registry import REGISTRY
from crm.testing import ZOHO_TEST_TOKEN, FakeResponse, HttpCall, HttpSpy, NetworkBlocked, NoNetworkTestCase
from crm.tests.test_schema_command import FakeZoho
from crm.zoho import KEY_FIELD
from quotes.intake import RequestMeta, save_submission
from quotes.models import GAQL_MODULE, QuoteSubmission
from quotes.tests.test_contract import load_fixture, normalised

TIMELINE_DIR = Path(__file__).resolve().parent / 'fixtures' / 'timeline'
GOLDEN_DIR = Path(__file__).resolve().parent / 'golden'

# The real records of the fixture timelines (understand/zoho-dumps).
GAQL_ID = '6686316000033717016'
APOSTILLE_COPY_ID = '6686316000033736004'
I9_COPY_ID = '6686316000030968002'
NOTARY_COPY_ID = '6686316000033749002'
COPY_TIME = '2026-11-26T15:19:10+06:00'
CONTACT_ID = '6686316000000900001'

ENGINE_SETTINGS = {
    'CRM_ENGINE_MODE': 'live', 'CRM_ENGINE_TEST_ONLY': True, 'CRM_SOURCES_ALLOWED': [GAQL_MODULE],
    'CRM_CREATE_LIMIT_PER_SOURCE_HOUR': 10, 'CRM_CREATE_LIMIT_GLOBAL_HOUR': 60, 'CRM_RULES_MAX_MODE': 'suggest',
    'INTAKE_CANDIDATE_DAYS': 30, 'ENGINE_INLINE': True, 'ENGINE_TID_CREATED_EMAIL': 'never',
    'ENGINE_TID_POLICY': {'Apostille_Services': 'mint_silent', 'Translation_Services': 'mint_silent',
                          'Embassy_Legalization': 'mint_silent', 'Deals': 'mint_silent'},
    'ENGINE_TRIGGER_POLICY': {'*': []}, 'ENGINE_PARITY_CONSTANTS': True, 'ENGINE_ADOPTION': 'auto_exact',
    'ENGINE_ORPHAN_SCAN': True, 'APOSTILLE_FBI_STAGE': '', 'ZOHO_OWNER_BY_SERVICE': {}, 'ZOHO_ORG_ID': 'org882204370',
    'QUOTE_AUTO_INTAKE': 'none', 'QUOTE_V3_ENABLED': False, 'QUOTE_V3_TEST_EMAILS': False, 'QUOTE_V3_SUBFORM': 'off',
    'QUOTE_V3_CONFIRMATION_EMAIL': False, 'OPS_ALERT_EMAILS': ['ops@example.com'],
}
engine_settings = override_settings(**ENGINE_SETTINGS)


def timeline_fixture(module: str, record_id: str) -> list[dict]:
    path = TIMELINE_DIR / f'facts-timeline-{module}-{record_id}.json'
    return json.loads(path.read_text(encoding='utf-8'))['__timeline']


def seed_metadata() -> FakeZoho:
    """The schema cache after ZOHO-SETUP (baseline + every API field + the manual steps)."""
    fake = FakeZoho()
    fake.create_api_fields()
    fake.add_option(GAQL_MODULE, 'GET_A_QUOTE_LEADS', 'Web Quote Request')
    fake.set_related_list_titles()
    for module, raw in fake.fields.items():
        cache.set(schema.CACHE_KEY.format(module=module), schema.by_api_name(schema.normalise(raw)),
                  schema.CACHE_TTL_S)
    return fake


def saved_quote(fixture: str = 'svc_apostille', *, zoho_record_id: str = GAQL_ID, contact_id: str = CONTACT_ID,
                body_changes=None, **fields) -> QuoteSubmission:
    """A v5 quote saved by the real intake from a contract fixture, as if the pipeline had synced it."""
    body = copy.deepcopy(load_fixture(fixture)['body'])
    body['submission_id'] = str(uuid.uuid4())
    if body_changes:
        body_changes(body)
    meta = RequestMeta(ip='10.0.0.1', user_agent='test', turnstile_result='skipped', attribution=None)
    quote = save_submission(normalised(body), meta).quote
    values = {'zoho_record_id': zoho_record_id, 'zoho_contact_id': contact_id, 'sync_status': 'synced',
              'zoho_lines_mode': 'text', **fields}
    QuoteSubmission.objects.filter(pk=quote.pk).update(**values)
    quote.refresh_from_db()
    return quote


def make_link(quote: QuoteSubmission, service: str, *, seq: int = 1, **fields) -> ServiceLink:
    """A ServiceLink of a v5 quote as `engine.handle` leaves it before its task runs (state pending)."""
    source_key = f'qr:{quote.pk}'
    values = {'state': 'pending', 'caller': 'zoho', 'trigger': 'zoho_button', **fields}
    return ServiceLink.objects.create(
        source_kind='quote', source_key=source_key, source_module=GAQL_MODULE, source_record_id=quote.zoho_record_id,
        quote=quote, service_key=service, seq=seq, external_key=external_key(source_key, service, seq),
        target_module=REGISTRY[service].module, **values)


_CRITERION = re.compile(r'\((\w+):equals:((?:\\.|[^\\)])*)\)')


def _unescape(value: str) -> str:
    return re.sub(r'\\(.)', r'\1', value)


class FakeCrm:
    """In-memory Zoho records API: records, search, create (DUPLICATE_DATA on the key), PUT, upsert, Notes,
    timelines, Contacts and the OAuth refresh. `fail[(method, module)]` serves queued answers first."""

    def __init__(self):
        self.records: dict[str, dict[str, dict]] = defaultdict(dict)
        self.timelines: dict[tuple[str, str], list[dict]] = {}
        self.notes: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.fail: dict[tuple[str, str], list] = defaultdict(list)
        self._ids = itertools.count(7100000000000000001)

    def next_id(self) -> str:
        return str(next(self._ids))

    def add(self, module: str, **fields) -> dict:
        record = {**fields, 'id': str(fields.get('id') or self.next_id())}
        self.records[module][record['id']] = record
        return record

    def load_timeline(self, module: str, record_id: str) -> list[dict]:
        events = timeline_fixture(module, record_id)
        self.timelines[(module, record_id)] = events
        return events

    def note_titles(self, module: str, record_id: str) -> list[str]:
        return [note['Note_Title'] for note in self.notes[(module, record_id)]]

    # --- routing -----------------------------------------------------------------------------------

    def __call__(self, call: HttpCall) -> FakeResponse:
        if call.url.startswith('https://accounts.zoho.com/oauth/'):
            return FakeResponse(200, {'access_token': 'refreshed-token'})
        path = call.url.removeprefix('https://www.zohoapis.com/crm/')
        _, _, rest = path.partition('/')
        parts = rest.split('/')
        queued = self.fail.get((call.method, parts[0]))
        if queued:
            answer = queued.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        route = (call.method, len(parts))
        if parts[0] == 'Notes' and call.method == 'POST':
            return self._add_note(call.json)
        if route == ('GET', 3) and parts[2] == '__timeline':
            return FakeResponse(200, {'__timeline': copy.deepcopy(self.timelines.get((parts[0], parts[1]), [])),
                                      'info': {'more_records': False}})
        if route == ('GET', 3) and parts[2] == 'Notes':
            notes = self.notes[(parts[0], parts[1])]
            return FakeResponse(200, {'data': copy.deepcopy(notes)}) if notes else FakeResponse(204)
        if route == ('GET', 2) and parts[1] == 'search':
            return self._search(parts[0], call.params or {})
        if route == ('GET', 2):
            record = self.records[parts[0]].get(parts[1])
            return FakeResponse(200, {'data': [copy.deepcopy(record)]}) if record else FakeResponse(204)
        if route == ('POST', 1):
            return self._create(parts[0], call.json['data'][0])
        if route == ('POST', 2) and parts[1] == 'upsert':
            return self._upsert(parts[0], call.json['data'][0])
        if route == ('PUT', 1):
            return self._update(parts[0], call.json['data'][0])
        if call.method == 'POST' and parts[2:] == ['actions', 'add_tags']:
            return FakeResponse(200, {'data': [{'code': 'SUCCESS', 'details': {'id': parts[1]}, 'status': 'success'}]})
        raise NetworkBlocked(f'FakeCrm has no route for {call.method} {call.url}')

    @staticmethod
    def _success(record_id: str, status: int = 200, **extra) -> FakeResponse:
        return FakeResponse(status, {'data': [{'code': 'SUCCESS', 'details': {'id': record_id}, 'status': 'success',
                                               **extra}]})

    def _search(self, module: str, params: dict) -> FakeResponse:
        if 'email' in params:
            terms = [('Email', params['email'])]
        else:
            terms = [(name, _unescape(value)) for name, value in _CRITERION.findall(params.get('criteria', ''))]
        found = [copy.deepcopy(record) for record in self.records[module].values()
                 if any(record.get(name) == value for name, value in terms)]
        return FakeResponse(200, {'data': found}) if found else FakeResponse(204)

    def _create(self, module: str, data: dict) -> FakeResponse:
        key = data.get(KEY_FIELD)
        if key:
            existing = next((r for r in self.records[module].values() if r.get(KEY_FIELD) == key), None)
            if existing is not None:
                return FakeResponse(400, {'data': [{'code': 'DUPLICATE_DATA', 'status': 'error', 'details': {
                    'api_name': KEY_FIELD, 'id': existing['id']}}]})
        record = self.add(module, **data, Created_Time=timezone.now().isoformat())
        return self._success(record['id'], 201)

    def _upsert(self, module: str, data: dict) -> FakeResponse:
        existing = next((r for r in self.records[module].values() if r.get(KEY_FIELD) == data.get(KEY_FIELD)), None)
        if existing is None:
            return self._success(self.add(module, **data)['id'], 201, action='insert')
        return self._success(existing['id'], action='update')

    def _update(self, module: str, data: dict) -> FakeResponse:
        record = self.records[module].get(str(data.get('id')))
        if record is None:
            return FakeResponse(400, {'data': [{'code': 'INVALID_DATA', 'status': 'error', 'details': {'api_name': 'id'}}]})
        for name, value in data.items():
            if name == 'id':
                continue
            if isinstance(value, list) and all(isinstance(row, dict) for row in value):
                value = [{**row, 'id': row.get('id') or self.next_id()} for row in value]   # subform rows
            record[name] = value
        return self._success(record['id'])

    def _add_note(self, body: dict) -> FakeResponse:
        data = body['data'][0]
        note = {'id': self.next_id(), 'Note_Title': data['Note_Title'], 'Note_Content': data['Note_Content']}
        self.notes[(data['se_module'], str(data['Parent_Id']))].append(note)
        return self._success(note['id'], 201)


class EngineHelpers:
    """Setup and assertions shared by the engine test cases (`self.http` is the NoNetwork spy)."""

    def engine_setup(self):
        self.warm_zoho_token()
        self.schema = seed_metadata()
        self.crm = FakeCrm()
        self.http.handler = self.crm

    def gaql_record(self, quote: QuoteSubmission, **fields) -> dict:
        """The GAQL record of a synced v5 quote, keyed `dcmn:qr:{pk}`."""
        return self.crm.add(GAQL_MODULE, id=quote.zoho_record_id, **{
            KEY_FIELD: f'dcmn:qr:{quote.pk}', 'Client_Name': quote.full_name, 'Client_Email': quote.email,
            'GET_A_QUOTE_LEADS': 'Web Quote Request', **fields})

    def zoho_writes(self) -> list[HttpCall]:
        """Writes to the CRM API (the OAuth refresh is not one)."""
        return [c for c in self.http.writes if c.url.startswith('https://www.zohoapis.com/')]

    def creates(self, module: str) -> list[HttpCall]:
        return [c for c in self.zoho_writes() if c.method == 'POST' and c.url.endswith(f'/v2/{module}')]

    def puts(self, module: str) -> list[HttpCall]:
        return [c for c in self.zoho_writes() if c.method == 'PUT' and c.url.endswith(f'/v2/{module}')]


@engine_settings
class EngineTestCase(EngineHelpers, NoNetworkTestCase):
    """Engine on (live, test-only), warm token, seeded metadata, `self.crm` answering every Zoho call."""

    def setUp(self):
        super().setUp()
        self.engine_setup()


@engine_settings
@override_settings(ZOHO_REFRESH_TOKEN='', ZOHO_CLIENT_ID='', ZOHO_CLIENT_SECRET='', TURNSTILE_SECRET_KEY='')
class EngineTransactionTestCase(EngineHelpers, TransactionTestCase):
    """EngineTestCase with committed rows, for races between DB connections (the same no-network spy)."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.http = HttpSpy()
        spy = self.http
        patcher = patch('requests.Session.request', new=lambda session, method, url, **kw: spy(method, url, **kw))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engine_setup()

    def warm_zoho_token(self) -> None:
        cache.set('zoho_access_token', ZOHO_TEST_TOKEN, 2900)
