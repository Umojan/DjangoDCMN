"""Adoption before create (DESIGN §7.6): own link, own key, timeline copy, strong heuristic, candidates, enrich."""

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from crm import adoption, engine, sources
from crm.adoption import Adoption, Candidates, Existing, find_existing
from crm.facts import from_quote
from crm.models import ServiceLink
from crm.registry import REGISTRY
from crm.tests.support import (
    APOSTILLE_COPY_ID, COPY_TIME, GAQL_ID, I9_COPY_ID, EngineTestCase, make_link, saved_quote, timeline_fixture,
)
from crm.writeback import write_back
from crm.zoho import KEY_FIELD
from quotes.models import GAQL_MODULE, QuotePhoneMatch

I9_STAGE = 'I-9 LEADS FROM GET QUOTE PIPELINE'
GAQL_ADDED = '2026-11-26T15:19:10+06:00'
SECOND_COPY_ID = '6686316000033736999'


def copy_rule_added(at: str, rule_id: str = '6686316000002342517') -> list[dict]:
    return [{'action': 'added', 'source': 'workflow', 'audited_time': at,
             'automation_details': {'rule': {'name': 'copy', 'id': rule_id}, 'type': 'workflow_rule'}}]


def iso(moment) -> str:
    return moment.isoformat()


class AdoptionTestCase(EngineTestCase):
    fixture = 'svc_apostille'
    service = 'apostille'

    def setUp(self):
        super().setUp()
        self.quote = saved_quote(self.fixture)
        self.record = self.gaql_record(self.quote, GET_A_QUOTE_LEADS='Apostille Service')
        self.spec = REGISTRY[self.service]

    def link(self, service: str | None = None, **fields) -> ServiceLink:
        return make_link(self.quote, service or self.service, **fields)

    def find(self, link: ServiceLink, *, record: dict | None = None, service: str | None = None):
        spec = REGISTRY[service or self.service]
        source = sources.for_quote(self.quote, record=record)
        return find_existing(link, spec, from_quote(self.quote, self.quote.lines.filter(
            service_key=spec.key).first()), source)

    def gaql_timeline(self, events: list[dict] | None = None) -> None:
        self.crm.timelines[(GAQL_MODULE, GAQL_ID)] = events if events is not None else [
            {'action': 'added', 'source': 'crm_api', 'audited_time': GAQL_ADDED}]

    def copy_record(self, module: str = 'Apostille_Services', **fields) -> dict:
        spec = next(s for s in REGISTRY.values() if s.module == module and s.copy_label)
        values = {'Name': spec.copy_label, spec.stage_field: spec.intake_stage(), 'Created_Time': COPY_TIME,
                  **{name: self.quote.email for name in spec.client.emails}, **fields}
        return self.crm.add(module, **values)


class OrderTests(AdoptionTestCase):
    def test_own_link_exists_then_target_deleted(self):
        record = self.crm.add('Apostille_Services', Name='Apostille · x')
        link = self.link(state='created', target_record_id=record['id'])

        self.assertEqual(self.find(link), Existing(record['id'], deleted=False))
        del self.crm.records['Apostille_Services'][record['id']]
        self.assertEqual(self.find(link), Existing(record['id'], deleted=True))
        # Step 1 asks for the id only, and never searches further.
        self.assertTrue(all(c.params == {'fields': 'id'} for c in self.http.calls))

        result = engine.handle(sources.for_quote(self.quote), ['apostille'], mode='create', trigger='zoho_button',
                               caller='zoho', actor={})
        self.assertEqual(result.body['results'][0]['status'], 'target_deleted')
        link.refresh_from_db()
        self.assertEqual(link.state, 'target_deleted')
        self.assertEqual(self.zoho_writes(), [])

    def test_key_search_adopts(self):
        link = self.link()
        record = self.crm.add('Apostille_Services', **{KEY_FIELD: link.external_key, 'Name': 'Apostille · x'})

        found = self.find(link)

        self.assertIsInstance(found, Adoption)
        self.assertEqual((found.record_id, found.origin), (record['id'], 'adopted_key'))
        self.assertEqual(engine.run_link(link.pk), 'adopted')
        link.refresh_from_db()
        self.assertEqual(link.origin, 'adopted_key')
        self.assertTrue(link.target_key_written)
        self.assertEqual(self.creates('Apostille_Services'), [])

    def test_timeline_fixture_adopts_6686316000033736004(self):
        self.crm.load_timeline(GAQL_MODULE, GAQL_ID)
        self.copy_record(id=APOSTILLE_COPY_ID)
        link = self.link()

        found = self.find(link)

        self.assertIsInstance(found, Adoption)
        self.assertEqual((found.record_id, found.origin, found.duplicates), (APOSTILLE_COPY_ID, 'adopted_copy', []))
        self.assertEqual(engine.run_link(link.pk), 'adopted')
        link.refresh_from_db()
        self.assertEqual((link.target_record_id, link.origin), (APOSTILLE_COPY_ID, 'adopted_copy'))
        self.assertEqual(link.target_url,
                         f'https://crm.zoho.com/crm/org882204370/tab/CustomModule2/{APOSTILLE_COPY_ID}')
        self.assertEqual(self.creates('Apostille_Services'), [])
        (put,) = self.puts('Apostille_Services')
        self.assertEqual(put.json['trigger'], [])
        self.assertEqual(put.json['data'][0]['id'], APOSTILLE_COPY_ID)
        self.assertEqual(put.json['data'][0][KEY_FIELD], link.external_key)

    def test_second_copy_gets_safe_to_delete_note(self):
        events = timeline_fixture(GAQL_MODULE, GAQL_ID)
        second = {**events[0], 'audited_time': '2026-11-26T15:21:10+06:00', 'automation_details': {
            **events[0]['automation_details'], 'create_record': {
                **events[0]['automation_details']['create_record'], 'id': SECOND_COPY_ID}}}
        self.gaql_timeline([second, *events])
        self.copy_record(id=APOSTILLE_COPY_ID)
        self.copy_record(id=SECOND_COPY_ID)
        link = self.link()

        found = self.find(link)
        self.assertEqual((found.record_id, found.duplicates), (APOSTILLE_COPY_ID, [SECOND_COPY_ID]))
        engine.run_link(link.pk)
        write_back(link.source_key)
        write_back(link.source_key)   # converges: no second Note

        titles = self.crm.note_titles('Apostille_Services', SECOND_COPY_ID)
        self.assertEqual(titles, [f'DCMN · {link.external_key} · duplicate copy'])
        (note,) = self.crm.notes[('Apostille_Services', SECOND_COPY_ID)]
        self.assertEqual(note['Note_Content'], f'Duplicate auto-copy of {self.quote.reference} — safe to delete')
        self.assertEqual(self.puts('Apostille_Services')[0].json['data'][0]['id'], APOSTILLE_COPY_ID)

    def test_qr_with_web_quote_bucket_skips_timeline(self):
        self.crm.load_timeline(GAQL_MODULE, GAQL_ID)
        self.copy_record(id=APOSTILLE_COPY_ID)
        link = self.link()

        found = self.find(link, record={**self.record, 'GET_A_QUOTE_LEADS': 'Web Quote Request'})

        self.assertFalse([c for c in self.http.calls if c.url.endswith('/__timeline')])
        self.assertNotIsInstance(found, Adoption)


class HeuristicTests(AdoptionTestCase):
    def test_adopts_only_with_target_workflow_rule_id(self):
        self.gaql_timeline()
        record = self.copy_record(Created_Time='2026-11-26T15:19:40+06:00')
        link = self.link()

        self.crm.timelines[('Apostille_Services', record['id'])] = copy_rule_added('2026-11-26T15:19:40+06:00',
                                                                                    rule_id='6686316000006992554')
        found = self.find(link)
        self.assertIsInstance(found, Candidates)   # a rule, but not a copy rule: never adopted
        self.assertEqual([(c['id'], c['match']) for c in found.items], [(record['id'], 'possible_copy')])

        self.crm.timelines[('Apostille_Services', record['id'])] = copy_rule_added('2026-11-26T15:19:40+06:00')
        found = self.find(link)
        self.assertEqual((found.record_id, found.origin), (record['id'], 'adopted_copy'))

        # The copy rule, but 10 minutes after the GAQL record changed: not the same event.
        late = self.copy_record(Created_Time='2026-11-26T15:29:40+06:00')
        del self.crm.records['Apostille_Services'][record['id']]
        self.crm.timelines[('Apostille_Services', late['id'])] = copy_rule_added('2026-11-26T15:29:40+06:00')
        self.assertIsInstance(self.find(link), Candidates)

    def test_two_candidates_needs_decision(self):
        self.gaql_timeline()
        first = self.copy_record()
        second = self.copy_record()
        for record in (first, second):
            self.crm.timelines[('Apostille_Services', record['id'])] = copy_rule_added(COPY_TIME)
        link = self.link()

        found = self.find(link)

        self.assertIsInstance(found, Candidates)
        self.assertEqual({c['id'] for c in found.items}, {first['id'], second['id']})
        self.assertEqual(engine.run_link(link.pk), 'needs_decision')
        link.refresh_from_db()
        self.assertEqual(len(link.candidates), 2)
        self.assertEqual(self.zoho_writes(), [])

    def test_i9_matches_by_phone(self):
        self.quote = saved_quote('svc_i9')
        self.gaql_record(self.quote, GET_A_QUOTE_LEADS='I-9 Employment Verification Services')
        self.gaql_timeline([{'action': 'added', 'source': 'crm_api', 'audited_time': '2026-10-22T20:08:05+06:00'}])
        record = self.crm.add('I_9_Verification', id=I9_COPY_ID, Name='I-9 Employment Verification Services',
                              Stage=I9_STAGE, Client_Email='someone.else@example.com',
                              Client_Phone=self.quote.phone_e164, Created_Time='2026-10-22T20:08:10+06:00')
        self.crm.load_timeline('I_9_Verification', I9_COPY_ID)
        link = self.link('i9')

        found = self.find(link, service='i9')

        self.assertEqual((found.record_id, found.origin), (record['id'], 'adopted_copy'))
        search = next(c for c in self.http.calls if c.url.endswith('/I_9_Verification/search')
                      and KEY_FIELD not in c.params['criteria'])
        self.assertIn('Client_Phone:equals:', search.params['criteria'])
        self.assertNotIn('Email', search.params['criteria'])

    def test_hidden_created_time_uses_timeline(self):
        self.quote = saved_quote('svc_translation')
        self.gaql_record(self.quote, GET_A_QUOTE_LEADS='Translation Service')
        self.gaql_timeline()
        self.assertEqual(adoption.created_field('Translation_Services'), 'Modified_Time')
        record = self.copy_record('Translation_Services', Created_Time=None,
                                  Modified_Time='2026-11-27T09:00:00+06:00')   # edited later: not the copy time
        link = self.link('translation')

        self.crm.timelines[('Translation_Services', record['id'])] = copy_rule_added(
            '2026-11-26T15:19:30+06:00', rule_id='6686316000002351021')
        found = self.find(link, service='translation')
        self.assertEqual((found.record_id, found.origin), (record['id'], 'adopted_copy'))

        self.crm.timelines[('Translation_Services', record['id'])] = copy_rule_added(
            '2026-11-26T15:49:30+06:00', rule_id='6686316000002351021')
        self.assertIsInstance(self.find(link, service='translation'), Candidates)

    def test_created_time_hidden_uses_modified_time_window(self):
        self.quote = saved_quote('svc_translation')
        self.gaql_record(self.quote, GET_A_QUOTE_LEADS='Translation Service')
        self.gaql_timeline([])
        now = timezone.now()
        recent = self.crm.add('Translation_Services', Name='Translation · by hand', Email=self.quote.email,
                              Translation_Status='Client Placed Request', Modified_Time=iso(now - timedelta(days=10)))
        self.crm.add('Translation_Services', Name='Translation · old', Email=self.quote.email,
                     Translation_Status='Client Placed Request', Modified_Time=iso(now - timedelta(days=40)))
        link = self.link('translation')

        found = self.find(link, service='translation')

        self.assertIsInstance(found, Candidates)
        (item,) = found.items
        self.assertEqual((item['id'], item['match']), (recent['id'], 'same_client'))
        self.assertEqual(item['created'], recent['Modified_Time'])


class CandidateTests(AdoptionTestCase):
    def test_phone_lead_record_is_candidate_never_adopted(self):
        record = self.crm.add('Apostille_Services', Name='Phone lead', Status='Phone Call Received',
                              Created_Time=COPY_TIME)
        QuotePhoneMatch.objects.create(quote=self.quote, phone_lead_id=4242, direction='call_before_quote',
                                       match_on='phone', phone_record_module='Apostille_Services',
                                       phone_record_id=record['id'], action='candidate')
        self.gaql_timeline([])
        link = self.link()

        found = self.find(link)

        self.assertIsInstance(found, Candidates)
        self.assertEqual([(c['id'], c['match']) for c in found.items], [(record['id'], 'phone_lead')])
        self.assertEqual(engine.run_link(link.pk), 'needs_decision')
        self.assertEqual(self.zoho_writes(), [])

    def test_hand_made_open_record_within_30_days_is_candidate(self):
        now = timezone.now()
        recent = self.crm.add('Apostille_Services', Name='Apostille · by hand', Client_Email=self.quote.email,
                              Status='Initial Contact Made - SEND BY GMAIL', Created_Time=iso(now - timedelta(days=5)))
        self.crm.add('Apostille_Services', Name='Apostille · old', Email=self.quote.email,
                     Status='Initial Contact Made - SEND BY GMAIL', Created_Time=iso(now - timedelta(days=40)))
        self.gaql_timeline([])
        link = self.link()

        found = self.find(link)

        self.assertEqual([(c['id'], c['match'], c['stage']) for c in found.items],
                         [(recent['id'], 'same_client', 'Initial Contact Made - SEND BY GMAIL')])
        self.assertEqual(found.items[0]['url'], REGISTRY['apostille'].url(recent['id']))

    def test_closed_record_ignored(self):
        now = timezone.now()
        for stage in ('Cancelled', 'Client Lost ❌ (silent or chose another provider)',
                      'Completed ✅ (Send Review) (Automation Email)'):
            self.crm.add('Apostille_Services', Name='Apostille · closed', Client_Email=self.quote.email, Status=stage,
                         Created_Time=iso(now - timedelta(days=2)))
        self.gaql_timeline([])
        link = self.link()

        self.assertIsNone(self.find(link))
        self.assertEqual(engine.run_link(link.pk), 'created')


class EnrichTests(AdoptionTestCase):
    def setUp(self):
        super().setUp()
        self.enrich_link = self.link()

    def enrich(self, record: dict) -> dict:
        facts = from_quote(self.quote, self.quote.lines.get(service_key='apostille'))
        adoption.enrich(self.enrich_link, self.spec, facts, record)
        (put,) = self.puts('Apostille_Services')
        self.assertEqual(put.json['trigger'], [])
        return put.json['data'][0]

    def test_put_never_touches_stage_tracking_id_owner(self):
        record = self.copy_record(Tracking_ID='DC-AB12CD', Owner={'id': '6686316000000503001'}, Client_Name='',
                                  Country_of_Use='Italy')

        fields = self.enrich(record)

        self.assertFalse({'Status', 'Tracking_ID', 'Owner'} & set(fields))
        self.assertEqual(fields[KEY_FIELD], f'dcmn:qr:{self.quote.pk}:apostille')
        self.assertEqual(fields['Quote_Request'], {'id': GAQL_ID})
        self.assertEqual(fields['Quote_Reference'], self.quote.reference)
        self.assertTrue(fields['Created_From'].endswith(f'/tab/CustomModule7/{GAQL_ID}'))
        self.assertTrue(fields['Name'].startswith('ZZ TEST · Apostille · '))   # the bare copy label is replaced
        self.assertEqual(fields['Client_Name'], self.quote.full_name)          # blank → filled
        self.assertNotIn('Country_of_Use', fields)                              # set by the copy → kept

    def test_existing_key_kept(self):
        record = self.copy_record(**{KEY_FIELD: 'dcmn:apostille:943', 'Name': 'Apostille · hand made'})

        fields = self.enrich(record)

        self.assertNotIn(KEY_FIELD, fields)
        self.assertNotIn('Name', fields)

    def test_comments_prefixed_once(self):
        record = self.copy_record(Client_Comments='Copied from the quote by the workflow')

        fields = self.enrich(record)

        block, _, rest = fields['Client_Comments'].partition('\n\n')
        self.assertTrue(block.startswith(f'[{self.quote.reference}] Apostille · from web quote'))
        self.assertEqual(rest, 'Copied from the quote by the workflow')

        self.http.calls.clear()
        enriched = self.copy_record(Client_Comments=fields['Client_Comments'])
        self.assertNotIn('Client_Comments', self.enrich(enriched))


class ModeTests(AdoptionTestCase):
    @override_settings(ENGINE_ADOPTION='confirm_all')
    def test_confirm_all_sends_exact_to_needs_decision(self):
        self.crm.load_timeline(GAQL_MODULE, GAQL_ID)
        self.copy_record(id=APOSTILLE_COPY_ID)
        link = self.link()

        found = self.find(link)

        self.assertIsInstance(found, Candidates)
        self.assertEqual([(c['id'], c['match']) for c in found.items], [(APOSTILLE_COPY_ID, 'exact_copy')])
        self.assertEqual(engine.run_link(link.pk), 'needs_decision')
        self.assertEqual(self.zoho_writes(), [])

        with override_settings(ENGINE_ADOPTION='off'):
            self.assertIsNone(self.find(self.link('embassy'), service='embassy'))   # off: own key only, then create
