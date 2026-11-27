"""Write-back (DESIGN §7.9): the quote's line fields recomputed from Django, idempotent Notes, the per-source lock."""

from unittest.mock import patch

from django.core.cache import cache
from django.utils import timezone

from crm import schema
from crm.models import ServiceLink
from crm.registry import REGISTRY
from crm.schema_spec import GAQL, QUOTE_LINES
from crm.tasks import writeback_source
from crm.tests.support import GAQL_ID, EngineTestCase, make_link, saved_quote
from crm.writeback import LOCK_KEY, LOCK_TTL_S, WritebackBusy, service_progress, write_back
from quotes.models import GAQL_MODULE
from quotes.render import lines_text


def rename_to_test(body: dict) -> None:
    body['contact']['full_name'] = 'ZZ TEST Maria Fernández'


class WritebackTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.quote = saved_quote('v5_success', body_changes=rename_to_test)
        self.gaql_record(self.quote)
        self.source_key = f'qr:{self.quote.pk}'

    def created(self, service: str, **fields) -> ServiceLink:
        record = self.crm.add(REGISTRY[service].module, Name=f'{service} record')
        values = {'state': 'created', 'origin': 'created', 'target_record_id': record['id'],
                  'target_url': f'https://crm.zoho.com/crm/org882204370/tab/CustomModule2/{record["id"]}',
                  'done_at': timezone.now(), **fields}
        return make_link(self.quote, service, **values)

    def gaql_puts(self) -> list[dict]:
        return [call.json['data'][0] for call in self.puts(GAQL_MODULE)]

    def test_lines_text_progress_pending_recomputed(self):
        apostille = self.created('apostille')
        translation = make_link(self.quote, 'translation')

        write_back(self.source_key)

        fields = self.gaql_puts()[-1]
        self.assertEqual(fields['id'], GAQL_ID)
        self.assertEqual(fields['Quote_Lines_Text'],
                         lines_text(self.quote, {'apostille': apostille, 'translation': translation}))
        self.assertIn(' — CREATED → Apostille Services', fields['Quote_Lines_Text'])
        self.assertIn(' — CREATING', fields['Quote_Lines_Text'])
        self.assertEqual(fields['Services_Requested'], ['Apostille', 'Translation'])
        self.assertEqual(fields['Services_Created'], ['Apostille'])
        self.assertEqual((fields['Service_Progress'], fields['Lines_Pending']), ('Partially created', 1))
        self.assertEqual(self.puts(GAQL_MODULE)[-1].json['trigger'], [])

        ServiceLink.objects.filter(pk=translation.pk).update(state='created', target_record_id='1')
        write_back(self.source_key)

        fields = self.gaql_puts()[-1]
        self.assertEqual((fields['Service_Progress'], fields['Lines_Pending']), ('All created', 0))
        self.assertEqual(fields['Services_Created'], ['Apostille', 'Translation'])
        self.assertEqual(ServiceLink.objects.get(pk=apostille.pk).writeback['source_lines']['state'], 'done')

    def test_service_progress_rules(self):
        lines = list(self.quote.lines.order_by('position'))
        link = ServiceLink(service_key='apostille', state='created')
        dismissed = ServiceLink(service_key='translation', state='dismissed')
        self.assertEqual(service_progress([], lines), ('Not started', 2))
        self.assertEqual(service_progress([link], lines), ('Partially created', 1))
        self.assertEqual(service_progress([link, dismissed], lines), ('All created', 0))
        other = saved_quote('svc_other')
        self.assertEqual(service_progress([], list(other.lines.all())), ('Nothing to create', 0))

    def test_never_writes_quote_status(self):
        self.created('apostille')
        make_link(self.quote, 'translation', state='needs_decision', candidates=[{'id': '1', 'url': 'u'}])

        write_back(self.source_key)
        write_back(self.source_key)

        self.assertTrue(self.gaql_puts())
        for fields in self.gaql_puts():
            self.assertFalse({'Quote_Status', 'GET_A_QUOTE_LEADS', 'Owner', 'Client_Comments'} & set(fields))

    def test_notes_idempotent_by_title_prefix(self):
        link = self.created('apostille', actor={'initials': 'VL'}, trigger='zoho_button')

        write_back(self.source_key)
        # A second run that lost the saved note state (an overlap after the lock expired) finds the titles.
        ServiceLink.objects.filter(pk=link.pk).update(writeback={})
        write_back(self.source_key)

        source_titles = self.crm.note_titles(GAQL_MODULE, GAQL_ID)
        self.assertEqual(source_titles, [f'DCMN · {link.external_key} · created'])
        (source_note,) = self.crm.notes[(GAQL_MODULE, GAQL_ID)]
        self.assertTrue(source_note['Note_Content'].startswith(f'Apostille → created: {link.target_url} · by VL'))
        target_titles = self.crm.note_titles('Apostille_Services', link.target_record_id)
        self.assertEqual(target_titles, [f'DCMN · {link.external_key} · source'])
        link.refresh_from_db()
        self.assertEqual(link.writeback['source_note']['state'], 'done')
        self.assertEqual(len([c for c in self.zoho_writes() if c.url.endswith('/Notes')]), 2)

        # A later event of the same link is a new title, posted once.
        ServiceLink.objects.filter(pk=link.pk).update(state='failed', last_error_code='INVALID_DATA')
        write_back(self.source_key)
        write_back(self.source_key)
        self.assertEqual(self.crm.note_titles(GAQL_MODULE, GAQL_ID)[1:], [f'DCMN · {link.external_key} · failed'])

    def test_subform_full_array_with_ids(self):
        self.schema.add_subform(GAQL, QUOTE_LINES)
        cache.set(schema.CACHE_KEY.format(module=GAQL),
                  schema.by_api_name(schema.normalise(self.schema.fields[GAQL])), schema.CACHE_TTL_S)
        self.quote.zoho_lines_mode = 'subform'
        self.quote.save(update_fields=['zoho_lines_mode'])
        self.created('apostille')

        write_back(self.source_key)

        rows = self.gaql_puts()[-1]['Quote_Lines']
        self.assertEqual([(r['Service_Key'], r['Line_Status'], r['Line_Ref']) for r in rows], [
            ('apostille', 'Created', f'{self.quote.reference}/1'),
            ('translation', 'Requested', f'{self.quote.reference}/2')])
        self.assertTrue(all('id' not in row for row in rows))   # the first write: Zoho assigns the row ids
        self.assertTrue(rows[0]['Service_Record'].endswith(ServiceLink.objects.get(service_key='apostille')
                                                            .target_record_id))
        ids = dict(self.quote.lines.values_list('service_key', 'zoho_row_id'))
        self.assertTrue(all(ids.values()))

        make_link(self.quote, 'translation')
        write_back(self.source_key)

        rows = self.gaql_puts()[-1]['Quote_Lines']
        self.assertEqual([(r['Service_Key'], r['id']) for r in rows],
                         [('apostille', ids['apostille']), ('translation', ids['translation'])])
        self.assertEqual(rows[1]['Line_Status'], 'Creating')   # the full array, every row, every time

    def test_lock_ttl_60_owner_release(self):
        lock = LOCK_KEY.format(source_key=self.source_key)
        self.assertEqual(LOCK_TTL_S, 60)
        with patch('crm.writeback.cache.add', wraps=cache.add) as add:
            write_back(self.source_key)
        add.assert_called_once()
        key, owner, ttl = add.call_args.args
        self.assertEqual((key, ttl), (lock, 60))
        self.assertIsNone(cache.get(lock))   # released by its owner

        def overrun(source_key):
            cache.set(lock, 'next-owner', 60)   # our TTL ran out and another run took the lock

        with patch('crm.writeback._write_back', side_effect=overrun):
            write_back(self.source_key)
        self.assertEqual(cache.get(lock), 'next-owner')   # never released by a non-owner

        calls = len(self.http.calls)
        with self.assertRaises(WritebackBusy):
            write_back(self.source_key)
        self.assertEqual(len(self.http.calls), calls)

    def test_lock_requeues_until_a_later_holder_covers_it(self):
        # Regression (review 2026-11-27, correctness-2): a second busy answer used to drop the write-back while
        # the first holder could still be running on links it read before this change.
        cache.add(LOCK_KEY.format(source_key=self.source_key), 'other', 60)

        with patch.object(writeback_source, 'apply_async') as requeue:
            self.assertEqual(writeback_source(self.source_key), 'requeued')
            requeue.assert_called_with((self.source_key,), {'requeued': 1}, countdown=5)
            self.assertEqual(writeback_source(self.source_key, requeued=True), 'requeued')   # an old message
            requeue.assert_called_with((self.source_key,), {'requeued': 2}, countdown=LOCK_TTL_S + 5)
            self.assertEqual(writeback_source(self.source_key, requeued=2), 'busy')   # a later holder has it
            self.assertEqual(requeue.call_count, 2)
        self.assertNoHttp()

    def test_legacy_source_notes_only(self):
        legacy = self.crm.add(GAQL_MODULE, Client_Name='ZZ TEST legacy', GET_A_QUOTE_LEADS='Apostille Service')
        record = self.crm.add('Apostille_Services', Name='Apostille · legacy')
        link = ServiceLink.objects.create(
            source_kind='zoho', source_key=f'rel:gaql:{legacy["id"]}', source_module=GAQL_MODULE,
            source_record_id=legacy['id'], service_key='apostille',
            external_key=f'dcmn:rel:gaql:{legacy["id"]}:apostille', target_module='Apostille_Services',
            target_record_id=record['id'], target_url='https://crm.zoho.com/x', state='created', origin='created',
            caller='zoho', trigger='zoho_button')

        write_back(link.source_key)

        self.assertEqual(self.puts(GAQL_MODULE), [])
        self.assertEqual({c.url.rsplit('/', 1)[-1] for c in self.zoho_writes()}, {'Notes'})
        self.assertEqual(self.crm.note_titles(GAQL_MODULE, legacy['id']), [f'DCMN · {link.external_key} · created'])
        (target_note,) = self.crm.notes[('Apostille_Services', record['id'])]
        self.assertIn('Created from the source record', target_note['Note_Content'])
