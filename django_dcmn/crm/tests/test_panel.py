"""The "Add service…" panel and the Plan B staff panel (DESIGN §8.2 options table, §8.4, §9.4)."""

import time
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import Client, override_settings
from django.urls import reverse

from crm import messages, zoho
from crm.models import EngineRequest, ServiceLink
from crm.panel import options as panel_options
from crm.panel.tokens import COOKIE_NAME, make_panel_url
from crm.panel.views import EXPIRED, INITIALS_REQUIRED, STALE_PANEL, USED
from crm.tests.support import make_link, saved_quote
from crm.tests.test_auth import ApiTestCase
from crm.zoho import KEY_FIELD
from quotes.models import GAQL_MODULE, QuoteSubmission

ZOHO_USER = '6686316000000503001'
MANUAL_ID = '6686316000066600001'
OTHER_ID = '6686316000066600002'
VALID_STAGE = 'FBI - Federal Apostille Requests'


class PanelTestCase(ApiTestCase):
    act_url = reverse('crm_panel_act')

    def panel_path(self) -> str:
        url = make_panel_url({'module': GAQL_MODULE, 'id': self.quote.zoho_record_id}, {'zoho_user_id': ZOHO_USER})
        return url.removeprefix(settings.BASE_URL)

    def open_panel(self, client=None):
        response = (client or self.client).get(self.panel_path())
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.panel_ref = response.context['urls']['ref']   # what the page's forms post back
        return response

    def act(self, url=None, client=None, **data):
        if url is None and hasattr(self, 'panel_ref'):
            data.setdefault('panel_ref', self.panel_ref)
        with self.captureOnCommitCallbacks(execute=True):
            return (client or self.client).post(url or self.act_url, data)

    def candidate_link(self, **fields) -> ServiceLink:
        self.crm.add('Apostille_Services', id=MANUAL_ID, Name='ZZ TEST hand-made apostille',
                     Client_Email=self.quote.email)
        self.crm.add('Apostille_Services', id=OTHER_ID, Name='ZZ TEST unrelated apostille')
        candidates = [{'module': 'Apostille_Services', 'id': MANUAL_ID, 'name': 'ZZ TEST hand-made apostille',
                       'stage': 'Client placed the request', 'created': '2026-11-20', 'url': 'https://crm.zoho.com/x',
                       'match': 'email'}]
        return make_link(self.quote, 'apostille', state='needs_decision', candidates=candidates, **fields)


class CookieTests(PanelTestCase):
    def test_cookie_path_httponly_secure_samesite_strict(self):
        response = self.open_panel()

        cookie = response.cookies[COOKIE_NAME]
        self.assertEqual(cookie['path'], '/crm/panel/')
        self.assertTrue(cookie['httponly'])
        self.assertTrue(cookie['secure'])
        self.assertEqual(cookie['samesite'], 'Strict')
        self.assertEqual(int(cookie['max-age']), 1800)
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)   # the Django session is not used
        self.assertContains(response, 'history.replaceState')
        self.assertContains(response, 'value="triple-seal"')
        self.assertContains(response, self.quote.reference)
        self.assertNotContains(response, 'name="force_spam"')
        again = self.client.get('/crm/panel/')   # the cookie alone re-renders the page
        self.assertEqual(again.status_code, 200)
        self.assertNotContains(again, 'history.replaceState')

    def test_token_reuse_refused(self):
        path = self.panel_path()
        self.assertEqual(self.client.get(path).status_code, 200)

        reused = Client().get(path)

        self.assertEqual(reused.status_code, 403)
        self.assertContains(reused, USED, status_code=403)
        self.assertNotIn(COOKIE_NAME, reused.cookies)

    def test_expired_token_refused(self):
        path = self.panel_path()

        with patch('django.core.signing.time.time', return_value=time.time() + 901):
            response = self.client.get(path)

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, EXPIRED, status_code=403)
        self.assertNotIn(COOKIE_NAME, response.cookies)
        self.assertEqual(Client().get('/crm/panel/').status_code, 403)   # no cookie, no panel


    def test_engine_off_closes_every_panel(self):
        path = self.panel_path()
        staff = f'{reverse("crm_panel_zoho")}?module={GAQL_MODULE}&id={self.quote.zoho_record_id}'

        with override_settings(CRM_ENGINE_MODE='off'):
            responses = [self.client.get(path), self.client.post(self.act_url, {'op': 'create:apostille'}),
                         self.client.get(staff)]

        for response in responses:
            self.assertContains(response, messages.DISABLED, status_code=503)
        self.assertEqual(self.client.get(path).status_code, 200)   # the link was not used up
        self.assertEqual(self.creates('Apostille_Services'), [])


class StaleWindowTests(PanelTestCase):
    def test_stale_window_cannot_act_on_another_record(self):
        # Regression (review 2026-11-27, security-panel-cookie-not-bound-to-tab): window A, then window B in the
        # same browser; a Create in window A used to run on B's record (the shared cookie names B).
        first = self.quote
        self.open_panel()
        window_a = self.panel_ref
        self.quote = saved_quote(zoho_record_id='6686316000099900001')
        self.gaql_record(self.quote)
        self.open_panel()
        window_b = self.panel_ref

        stale = self.act(op='create:embassy', initials='VL', panel_ref=window_a)
        self.assertEqual(stale.status_code, 409)
        self.assertContains(stale, STALE_PANEL, status_code=409)
        self.assertFalse(ServiceLink.objects.exists())
        self.assertEqual(self.creates('Embassy_Legalization'), [])
        missing = self.client.post(self.act_url, {'op': 'create:embassy', 'initials': 'VL'})
        self.assertEqual(missing.status_code, 409)   # a form without the reference is refused too

        poll = self.client.get('/crm/panel/', {'job': '00000000-0000-0000-0000-000000000000', 'ref': window_a})
        self.assertEqual((poll.status_code, poll.json()['detail']), (409, 'stale_panel'))

        current = self.act(op='create:embassy', initials='VL', panel_ref=window_b)
        self.assertEqual(current.status_code, 202)
        (link,) = ServiceLink.objects.all()
        self.assertEqual((link.source_key, link.source_record_id), (f'qr:{self.quote.pk}', self.quote.zoho_record_id))
        self.assertNotEqual(link.source_key, f'qr:{first.pk}')


class ActTests(PanelTestCase):
    def test_csrf_required(self):
        client = Client(enforce_csrf_checks=True)
        self.open_panel(client)

        refused = self.act(client=client, op='preview:apostille', initials='VL')
        accepted = self.act(client=client, op='preview:apostille', initials='VL',
                            csrfmiddlewaretoken=client.cookies[settings.CSRF_COOKIE_NAME].value)

        self.assertEqual((refused.status_code, accepted.status_code), (403, 200))

    def test_initials_required(self):
        self.open_panel()

        refused = self.act(op='create:apostille')

        self.assertEqual(refused.status_code, 400)
        self.assertContains(refused, INITIALS_REQUIRED, status_code=400)
        self.assertFalse(ServiceLink.objects.exists())
        self.assertEqual(self.zoho_writes(), [])
        self.assertEqual(self.act(op='create:apostille', initials='vlx').status_code, 202)
        link = ServiceLink.objects.get()
        self.assertEqual((link.state, link.caller, link.trigger, link.actor['initials']),
                         ('created', 'panel', 'panel', 'VLX'))
        self.assertContains(self.client.get('/crm/panel/'), 'value="VLX"')   # remembered in the cookie

    def test_link_existing_adopts_manual(self):
        link = self.candidate_link()
        self.open_panel()

        response = self.act(op=f'link:apostille:{MANUAL_ID}', initials='VL')

        self.assertEqual(response.status_code, 200)
        link.refresh_from_db()
        self.assertEqual((link.state, link.origin, link.target_record_id), ('adopted', 'adopted_manual', MANUAL_ID))
        self.assertEqual(self.crm.records['Apostille_Services'][MANUAL_ID][KEY_FIELD], link.external_key)
        self.assertEqual(self.creates('Apostille_Services'), [])

    def test_create_new_skips_candidates(self):
        link = self.candidate_link()
        self.open_panel()

        response = self.act(op='create_new:apostille', initials='VL')

        self.assertEqual(response.status_code, 202)
        link.refresh_from_db()
        self.assertEqual((link.state, link.origin, link.candidates), ('created', 'created', []))
        self.assertTrue(link.overrides['create_new'])
        self.assertEqual(len(self.creates('Apostille_Services')), 1)
        self.assertNotIn(link.target_record_id, (MANUAL_ID, OTHER_ID))

    def test_dismiss_suggestion(self):
        suggestion = make_link(self.quote, 'translation', state='suggested', caller='system', trigger='intake')
        self.open_panel()
        self.assertContains(self.client.get('/crm/panel/'), 'value="dismiss:translation"')

        response = self.act(op='dismiss:translation', initials='VL')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'suggestion dismissed')
        suggestion.refresh_from_db()
        self.assertEqual((suggestion.state, suggestion.caller, suggestion.actor['initials']),
                         ('dismissed', 'panel', 'VL'))
        audit = EngineRequest.objects.get(mode='dismiss')
        self.assertEqual((audit.caller, audit.state, suggestion.request_id), ('panel', 'done', audit.pk))
        self.assertEqual(self.creates('Translation_Services'), [])
        again = self.act(op='dismiss:translation', initials='VL')
        self.assertEqual(again.status_code, 409)

    def test_preview_no_writes(self):
        self.open_panel()
        writes = len(self.zoho_writes())

        response = self.act(op='preview:apostille', initials='VL')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'ZZ TEST · Apostille · {self.quote.full_name}')
        self.assertContains(response, 'value="create:apostille"')
        self.assertEqual(len(self.zoho_writes()), writes)
        self.assertFalse(ServiceLink.objects.exists())
        self.assertEqual(EngineRequest.objects.get(mode='preview').caller, 'panel')

    def test_stage_override_limited_to_registry_values(self):
        self.open_panel()

        bogus = self.act(op='create:apostille', initials='VL', stage='Closed Won')
        automated = self.act(op='create:apostille', initials='VL', stage='AUTOMATED - BY CRM')

        self.assertEqual((bogus.status_code, automated.status_code), (403, 403))
        self.assertFalse(ServiceLink.objects.exists())
        self.assertEqual(EngineRequest.objects.filter(error_code='option_not_allowed', caller='panel').count(), 2)
        allowed = self.act(op='create:apostille', initials='VL', stage=VALID_STAGE)
        self.assertEqual(allowed.status_code, 202)
        (post,) = self.creates('Apostille_Services')
        self.assertEqual(post.json['data'][0]['Status'], VALID_STAGE)


class TokenPanelLimitTests(PanelTestCase):
    def test_token_panel_refuses_force_spam_and_owner_override(self):
        QuoteSubmission.objects.filter(pk=self.quote.pk).update(spam_status='spam')
        self.open_panel()

        spam = self.act(op='create:apostille', initials='VL', force_spam='1')
        owner = self.act(op='create:apostille', initials='VL', owner_id='6686316000000999999')

        self.assertEqual((spam.status_code, owner.status_code), (403, 403))
        self.assertContains(spam, 'force_spam', status_code=403)
        self.assertContains(owner, 'overrides.owner_id', status_code=403)
        self.assertContains(owner, 'staff panel', status_code=403)
        refused = EngineRequest.objects.filter(caller='panel', state='refused', error_code='option_not_allowed')
        self.assertEqual(refused.count(), 2)
        self.assertFalse(ServiceLink.objects.exists())
        self.assertEqual(self.creates('Apostille_Services'), [])

    def test_token_panel_links_only_listed_candidates(self):
        link = self.candidate_link()
        self.open_panel()

        unlisted = self.act(op=f'link:apostille:{OTHER_ID}', initials='VL')

        self.assertEqual(unlisted.status_code, 403)
        link.refresh_from_db()
        self.assertEqual(link.state, 'needs_decision')
        self.assertEqual(self.act(op=f'link:apostille:{MANUAL_ID}', initials='VL').status_code, 200)
        link.refresh_from_db()
        self.assertEqual((link.state, link.target_record_id), ('adopted', MANUAL_ID))

    def test_token_panel_create_again_only_when_target_verified_deleted(self):
        self.open_panel()
        self.act(op='create:apostille', initials='VL')
        first = ServiceLink.objects.get()

        still_there = self.act(op='create_again:apostille', initials='VL')
        self.assertEqual(still_there.status_code, 403)
        self.assertContains(still_there, 'the record still exists', status_code=403)
        self.assertEqual(ServiceLink.objects.count(), 1)

        del self.crm.records['Apostille_Services'][first.target_record_id]   # a fresh GET now answers 204
        again = self.act(op='create_again:apostille', initials='VL')
        self.assertEqual(again.status_code, 202)
        second = ServiceLink.objects.get(seq=2)
        self.assertEqual(second.state, 'created')
        self.assertNotEqual(second.target_record_id, first.target_record_id)

        # once per link per hour, and a Zoho error is a refusal
        del self.crm.records['Apostille_Services'][second.target_record_id]
        panel_options.check_options('panel', {'create_again': True}, second)
        with self.assertRaisesMessage(panel_options.OptionNotAllowed, 'already created again in the last hour'):
            panel_options.check_options('panel', {'create_again': True}, second)
        third = make_link(self.quote, 'apostille', seq=3, state='target_deleted', target_record_id=OTHER_ID)
        with patch.object(zoho, 'get', side_effect=zoho.ZohoTransient('down')), \
                self.assertRaisesMessage(panel_options.OptionNotAllowed, 'could not confirm'):
            panel_options.check_options('panel', {'create_again': True}, third)


class PlanBTests(PanelTestCase):
    def staff_url(self) -> str:
        return f'{reverse("crm_panel_zoho")}?module={GAQL_MODULE}&id={self.quote.zoho_record_id}'

    def staff_user(self, *, staff=True, group=True) -> User:
        user = User.objects.create_user(f'user{User.objects.count()}', email='lead@example.com', password='unused',
                                        is_staff=staff)
        if group:
            user.groups.add(Group.objects.get_or_create(name='crm_panel')[0])
        return user

    def test_requires_staff_login_in_group(self):
        anonymous = self.client.get(self.staff_url())
        self.assertEqual(anonymous.status_code, 403)
        self.assertContains(anonymous, reverse('admin:login'), status_code=403)

        for user, status in ((self.staff_user(staff=False), 403), (self.staff_user(group=False), 403),
                             (self.staff_user(), 200)):
            client = Client()
            client.force_login(user)
            with self.subTest(staff=user.is_staff, groups=list(user.groups.values_list('name', flat=True))):
                self.assertEqual(client.get(self.staff_url()).status_code, status)
        self.assertEqual(self.act(url=self.staff_url(), op='create:apostille').status_code, 403)   # anonymous POST
        self.assertFalse(ServiceLink.objects.exists())

    def test_staff_panel_allows_staff_options(self):
        QuoteSubmission.objects.filter(pk=self.quote.pk).update(spam_status='spam')
        self.client.force_login(self.staff_user())
        page = self.client.get(self.staff_url())
        self.assertContains(page, 'name="force_spam"')
        self.assertContains(page, 'name="owner_id"')

        created = self.act(url=self.staff_url(), op='create:apostille', force_spam='1',
                           owner_id='6686316000000999999')

        self.assertEqual(created.status_code, 202)
        (post,) = self.creates('Apostille_Services')
        self.assertEqual(post.json['data'][0]['Owner'], {'id': '6686316000000999999'})
        first = ServiceLink.objects.get()
        again = self.act(url=self.staff_url(), op='create_again:apostille', force_spam='1')   # the old record exists
        self.assertEqual(again.status_code, 202)
        self.assertEqual(ServiceLink.objects.filter(seq=2, state='created').count(), 1)
        self.crm.add('Embassy_Legalization', id=OTHER_ID, Name='ZZ TEST any embassy record')
        linked = self.act(url=self.staff_url(), op='link:embassy', record_id=OTHER_ID, force_spam='1')
        self.assertEqual(linked.status_code, 200)
        self.assertEqual(ServiceLink.objects.get(service_key='embassy').target_record_id, OTHER_ID)
        self.assertNotEqual(first.pk, ServiceLink.objects.get(seq=2).pk)

    def test_staff_actor_recorded(self):
        user = self.staff_user()
        self.client.force_login(user)

        self.act(url=self.staff_url(), op='create:apostille', initials='AZ')

        request = EngineRequest.objects.get(mode='create')
        expected = {'kind': 'staff', 'staff_user_id': user.pk, 'email': 'lead@example.com', 'initials': 'AZ'}
        self.assertEqual((request.caller, request.trigger, request.actor), ('staff', 'staff', expected))
        link = ServiceLink.objects.get()
        self.assertEqual((link.caller, link.actor), ('staff', expected))
