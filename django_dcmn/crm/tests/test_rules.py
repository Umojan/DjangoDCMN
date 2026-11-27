"""Rules (DESIGN §7.11): the effective mode and the approval gate of `create`."""

from django.core import mail
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from crm import rules
from crm.models import AutomationRule, ServiceLink, ServiceSignal
from crm.tests.support import EngineTestCase, saved_quote
from quotes.models import GAQL_MODULE


class RuleTests(EngineTestCase):
    def test_effective_mode_is_min_of_row_default_ceiling(self):
        self.assertEqual(rules.effective_mode('translation_from_form'), 'suggest')             # default
        self.assertEqual(rules.effective_mode('apostille_implied_from_quote'), 'suggest')
        self.assertEqual(rules.effective_mode('quote_accepted_create_remaining'), 'off')       # default off
        self.assertEqual(rules.effective_mode('no_such_rule'), 'off')

        AutomationRule.objects.create(key='translation_from_form', mode='dry_run')
        self.assertEqual(rules.effective_mode('translation_from_form'), 'dry_run')              # the row wins
        AutomationRule.objects.create(key='lead_qualified_create', mode='create', approved_by='Aziz',
                                      approved_at=timezone.now())
        self.assertEqual(rules.effective_mode('lead_qualified_create'), 'suggest')              # ceiling suggest
        with override_settings(CRM_RULES_MAX_MODE='create'):
            self.assertEqual(rules.effective_mode('lead_qualified_create'), 'create')
        with override_settings(CRM_RULES_MAX_MODE='dry_run'):
            self.assertEqual(rules.effective_mode('apostille_implied_from_quote'), 'dry_run')
        with override_settings(CRM_RULES_MAX_MODE='off'):
            self.assertEqual(rules.effective_mode('lead_qualified_create'), 'off')
        with override_settings(CRM_RULES_MAX_MODE='bogus'):
            self.assertEqual(rules.effective_mode('lead_qualified_create'), 'suggest')          # safe default
        self.assertEqual(mail.outbox, [])

    @override_settings(CRM_RULES_MAX_MODE='create')
    def test_create_without_approval_downgrades_and_alerts(self):
        rule = AutomationRule(key='apostille_implied_from_quote', mode='create')
        with self.assertRaises(ValidationError):
            rule.full_clean()
        rule.save()   # bypassing the admin (a shell or a migration) must not make it create

        self.assertEqual(rules.effective_mode('apostille_implied_from_quote', alert=False), 'suggest')
        self.assertEqual(mail.outbox, [])
        self.assertEqual(rules.effective_mode('apostille_implied_from_quote'), 'suggest')
        (alert,) = mail.outbox
        self.assertIn('without approval', alert.subject)

        quote = saved_quote('svc_fbi')
        signal = ServiceSignal.objects.create(
            dedupe_key=f'apostille_implied:qr:{quote.pk}:apostille', kind='apostille_implied', source_kind='quote',
            source_key=f'qr:{quote.pk}', source_module=GAQL_MODULE, source_record_id=quote.zoho_record_id,
            quote=quote, service_key='apostille')

        self.assertEqual(rules.evaluate(signal), 'suggested')

        link = ServiceLink.objects.get()
        self.assertEqual((link.state, link.reason, link.caller), ('suggested', 'signal:apostille_implied_from_quote',
                                                                  'rules'))
        signal.refresh_from_db()
        self.assertEqual((signal.state, signal.outcome, signal.rule_key, signal.attempts),
                         ('evaluated', 'suggested', 'apostille_implied_from_quote', 1))
        self.assertNoHttp()
