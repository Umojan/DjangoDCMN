"""Phone leads before and after a v3 quote (DESIGN §5.7, option b); PhoneCallLead is never written (WP4)."""

import itertools
from datetime import timedelta
from pathlib import Path

from django.utils import timezone

import crm
from crm.models import ServiceLink
from crm.schema_spec import GAQL
from crm.testing import FakeResponse
from orders.models import PhoneCallLead
from quotes import emails, phone_match
from quotes.models import QuotePhoneMatch, QuoteSubmission
from quotes.render import ET
from quotes.tests.test_pipeline import PipelineTestCase, v5_quote

_lead_ids = itertools.count(1)
ORG = 'org882204370'


def phone_lead(*, phone: str = '', email: str = '', module: str = GAQL, record_id: str = '6686316000055500009',
               created_at=None, **fields) -> PhoneCallLead:
    """A synced WhatConverts lead (created through the ORM, as the webhook does)."""
    lead = PhoneCallLead.objects.create(
        whatconverts_lead_id=f'wc-{next(_lead_ids)}', contact_phone=phone, contact_email=email or None,
        zoho_module=module, zoho_lead_id=record_id, zoho_synced=bool(record_id), call_duration=170,
        landing_url='https://www.dcmobilenotary.com/apostille?gclid=x', raw_webhook_data={}, **fields,
    )
    if created_at is not None:
        PhoneCallLead.objects.filter(pk=lead.pk).update(created_at=created_at)
        lead.refresh_from_db()
    return lead


def synced(quote: QuoteSubmission, record_id: str = '6686316000044400001') -> QuoteSubmission:
    QuoteSubmission.objects.filter(pk=quote.pk).update(sync_status='synced', zoho_record_id=record_id)
    quote.refresh_from_db()
    return quote


class MatchTests(PipelineTestCase):
    def notes_on(self, record_id: str) -> list[dict]:
        return [call.json['data'][0] for call in self.calls('POST', 'Notes')
                if call.json['data'][0]['Parent_Id'] == record_id]

    def test_before_quote_creates_match_and_notes(self):
        quote = synced(v5_quote())
        day_before = quote.created_at - timedelta(days=1)
        lead = phone_lead(phone='(202) 555-0147', record_id='6686316000055500001', created_at=day_before)
        phone_lead(phone='(202) 555-0147', created_at=quote.created_at - timedelta(days=15))   # too old
        phone_lead(phone='(202) 555-0999', created_at=day_before)                              # another client
        phone_lead(phone='(202) 555-0147', record_id='', created_at=day_before)                # never synced
        by_email = phone_lead(email='MARIA@example.com ', record_id='6686316000055500002',
                              created_at=day_before + timedelta(minutes=5))

        matches = phone_match.match_before(quote)

        self.assertEqual([(m.phone_lead_id, m.match_on) for m in matches], [(lead.pk, 'phone'), (by_email.pk, 'email')])
        match = QuotePhoneMatch.objects.get(phone_lead=lead)
        self.assertEqual((match.direction, match.action, match.phone_record_module, match.phone_record_id),
                         ('call_before_quote', 'noted', GAQL, '6686316000055500001'))
        self.assertEqual(match.whatconverts_lead_id, lead.whatconverts_lead_id)
        self.assertEqual(len(match.note_ids), 2)

        on_quote = self.notes_on(quote.zoho_record_id)
        on_phone = self.notes_on('6686316000055500001')
        day = day_before.astimezone(ET)
        self.assertEqual(on_quote[0]['Note_Content'],
                         f'Client called on {day.day} {day:%b} (3 min, landing /apostille). Phone lead: '
                         f'https://crm.zoho.com/crm/{ORG}/tab/CustomModule7/6686316000055500001')
        self.assertEqual(on_phone[0]['Note_Content'],
                         f'Web quote {quote.reference} has the details: {quote.zoho_url}')
        self.assertEqual(on_phone[0]['se_module'], GAQL)
        self.assertNotIn(quote.full_name, on_quote[0]['Note_Content'] + on_phone[0]['Note_Content'])

        posts = len(self.calls('POST', 'Notes'))
        self.assertEqual(len(phone_match.match_before(quote)), 2)    # a re-run: no new row, no new Note
        self.assertEqual(len(self.calls('POST', 'Notes')), posts)
        self.assertEqual(QuotePhoneMatch.objects.count(), 2)

    def test_phone_call_lead_matched_fields_unchanged(self):
        quote = synced(v5_quote())
        before = phone_lead(phone='202 555 0147', created_at=quote.created_at - timedelta(hours=2))
        after = phone_lead(phone='202 555 0147', record_id='6686316000055500003')
        snapshot = {lead.pk: (lead.matched_with_form, lead.matched_order_type, lead.matched_order_id,
                              lead.updated_at, lead.zoho_lead_id, lead.zoho_module) for lead in (before, after)}

        phone_match.match_before(quote)
        phone_match.link_recent()

        self.assertEqual(QuotePhoneMatch.objects.count(), 2)
        for lead in PhoneCallLead.objects.all():
            self.assertEqual((lead.matched_with_form, lead.matched_order_type, lead.matched_order_id,
                              lead.updated_at, lead.zoho_lead_id, lead.zoho_module), snapshot[lead.pk])

    def test_phone_record_in_target_module_becomes_candidate(self):
        quote = synced(v5_quote())
        earlier = quote.created_at - timedelta(days=2)
        at_stage = phone_lead(phone='2025550147', module='Apostille_Services', record_id='6686316000055500010',
                              created_at=earlier)
        moved_on = phone_lead(email='maria@example.com', module='Apostille_Services', record_id='6686316000055500011',
                              created_at=earlier)
        deal = phone_lead(email='maria@example.com', module='Deals', record_id='6686316000055500012',
                          created_at=earlier)
        self.crm.records[('Apostille_Services', '6686316000055500010')] = {'id': '1', 'Status': 'Phone Call Received'}
        self.crm.records[('Apostille_Services', '6686316000055500011')] = {'id': '2', 'Status': 'Document Received'}

        phone_match.match_before(quote)

        actions = dict(QuotePhoneMatch.objects.values_list('phone_lead_id', 'action'))
        self.assertEqual(actions, {at_stage.pk: 'candidate', moved_on.pk: 'noted', deal.pk: 'noted'})
        stage_get = [c for c in self.http.calls if c.method == 'GET' and '/Apostille_Services/' in c.url
                     and not c.url.endswith('/Notes')]
        self.assertEqual({c.params['fields'] for c in stage_get}, {'Status'})
        self.assertEqual([key for key, _ in phone_match.candidate_lines(quote)], ['apostille'])
        self.assertFalse(ServiceLink.objects.exists())    # a candidate creates nothing
        links = [link['label'] for link in emails.staff_links(quote)]
        self.assertIn('Client also called — a phone-lead Apostille record exists: link it or create new in '
                      'Add service…', links)

    def test_link_recent_after_quote(self):
        quote = synced(v5_quote())
        QuoteSubmission.objects.filter(pk=quote.pk).update(created_at=timezone.now() - timedelta(days=2))
        quote.refresh_from_db()
        lead = phone_lead(email='maria@example.com', record_id='6686316000055500020')
        phone_lead(email='maria@example.com', created_at=timezone.now() - timedelta(hours=25))       # too old
        old_quote = synced(v5_quote(), record_id='6686316000044400002')
        QuoteSubmission.objects.filter(pk=old_quote.pk).update(created_at=timezone.now() - timedelta(days=31))

        self.assertEqual(phone_match.link_recent(), 1)

        match = QuotePhoneMatch.objects.get()
        self.assertEqual((match.quote_id, match.phone_lead_id, match.direction, match.match_on),
                         (quote.pk, lead.pk, 'call_after_quote', 'email'))
        today = lead.created_at.astimezone(ET)
        (on_quote,) = self.notes_on(quote.zoho_record_id)
        self.assertEqual(on_quote['Note_Content'], f'Client called again on {today.day} {today:%b} (3 min). '
                         f'Phone lead: https://crm.zoho.com/crm/{ORG}/tab/CustomModule7/6686316000055500020')
        (on_phone,) = self.notes_on('6686316000055500020')
        self.assertEqual(on_phone['Note_Content'],
                         f'Follow-up call to web quote {quote.reference}: {quote.zoho_url}. '
                         'Probably not a new request — check before working it')

        self.assertEqual(phone_match.link_recent(), 0)       # linked once
        self.assertEqual(len(self.calls('POST', 'Notes')), 2)

    def test_link_recent_resumes_after_zoho_error(self):
        quote = synced(v5_quote())
        QuoteSubmission.objects.filter(pk=quote.pk).update(created_at=timezone.now() - timedelta(hours=1))
        lead = phone_lead(phone='2025550147', record_id='6686316000055500025')
        self.crm.fail[('GET', f'{GAQL}/6686316000055500025/Notes')] = FakeResponse(503, {'code': 'INTERNAL_ERROR'})

        self.assertEqual(phone_match.link_recent(), 0)
        self.assertEqual(QuotePhoneMatch.objects.get(phone_lead=lead).action, '')

        del self.crm.fail[('GET', f'{GAQL}/6686316000055500025/Notes')]
        self.assertEqual(phone_match.link_recent(), 1)
        match = QuotePhoneMatch.objects.get(phone_lead=lead)
        self.assertEqual((match.action, len(match.note_ids)), ('noted', 2))
        self.assertEqual(len(self.notes_on(quote.zoho_record_id)), 1)     # the first Note was not posted twice

    def test_call_after_quote_record_becomes_candidate_or_note(self):
        quote = synced(v5_quote())
        QuoteSubmission.objects.filter(pk=quote.pk).update(created_at=timezone.now() - timedelta(hours=3))
        quote.refresh_from_db()
        first = phone_lead(phone='2025550147', module='Apostille_Services', record_id='6686316000055500030')
        self.crm.records[('Apostille_Services', '6686316000055500030')] = {'id': '1', 'Status': 'Phone Call Received'}

        phone_match.link_recent()
        self.assertEqual(QuotePhoneMatch.objects.get(phone_lead=first).action, 'candidate')

        other = synced(v5_quote(), record_id='6686316000044400003')
        QuoteSubmission.objects.filter(pk=other.pk).update(created_at=timezone.now() - timedelta(hours=2),
                                                           phone_last10='2025550148', email_normalized='o@example.com')
        ServiceLink.objects.create(source_kind='quote', source_key=f'qr:{other.pk}', quote=other,
                                   service_key='apostille', external_key=f'dcmn:qr:{other.pk}:apostille',
                                   state='created', target_module='Apostille_Services', target_record_id='9')
        second = phone_lead(phone='2025550148', module='Apostille_Services', record_id='6686316000055500031')
        self.crm.records[('Apostille_Services', '6686316000055500031')] = {'id': '2', 'Status': 'Phone Call Received'}

        phone_match.link_recent()

        match = QuotePhoneMatch.objects.get(phone_lead=second)
        self.assertEqual((match.quote_id, match.action), (other.pk, 'noted'))   # the line already has its record
        self.assertEqual(len(match.note_ids), 2)

    def test_followup_call_counted_in_digest(self):
        quote = synced(v5_quote())
        QuoteSubmission.objects.filter(pk=quote.pk).update(created_at=timezone.now() - timedelta(days=1))
        quote.refresh_from_db()
        phone_lead(phone='2025550147', record_id='6686316000055500040')
        phone_lead(email='maria@example.com', record_id='6686316000055500041', created_at=timezone.now())
        phone_lead(phone='2025550147', record_id='6686316000055500042',
                   created_at=quote.created_at - timedelta(hours=1))

        phone_match.match_before(quote)
        phone_match.link_recent()

        self.assertEqual(QuotePhoneMatch.objects.filter(direction='call_before_quote').count(), 1)
        today = timezone.now().astimezone(ET).date()
        self.assertEqual(phone_match.followup_calls_on(today), 2)
        self.assertEqual(phone_match.followup_calls_on(today - timedelta(days=1)), 0)

    def test_phone_lead_delete_not_blocked_by_match(self):
        quote = synced(v5_quote())
        lead = phone_lead(phone='2025550147', created_at=quote.created_at - timedelta(hours=1))
        phone_match.match_before(quote)
        match = QuotePhoneMatch.objects.get()

        lead.delete()

        match.refresh_from_db()
        self.assertEqual(match.whatconverts_lead_id, lead.whatconverts_lead_id)
        self.assertIsNone(PhoneCallLead.objects.filter(pk=match.phone_lead_id).first())
        self.assertEqual(phone_match.candidate_lines(quote), [])
        self.assertIsInstance(emails.staff_links(quote), list)


class ModuleMapTests(PipelineTestCase):
    def test_every_quote_service_has_a_target_or_none(self):
        from quotes import contract

        self.assertEqual(set(phone_match.SERVICE_TARGET_MODULES) | contract.NO_TARGET_SERVICES,
                         set(contract.SERVICE_KEYS))
        registry = Path(crm.__file__).resolve().parent / 'registry.py'
        if registry.exists():       # WP5: the engine registry is the source of truth
            from crm.registry import REGISTRY

            for key, module in phone_match.SERVICE_TARGET_MODULES.items():
                self.assertEqual(REGISTRY[key].module, module, key)
                self.assertEqual(REGISTRY[key].tab, phone_match.PHONE_LEAD_MODULES.get(module, (REGISTRY[key].tab,))[0])
