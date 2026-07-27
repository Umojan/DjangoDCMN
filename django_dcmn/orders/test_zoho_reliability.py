from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from .models import (
    FbiApostilleOrder,
    FbiServicePackage,
    PhoneCallLead,
    QuoteRequest,
    QuoteRequestDeduplication,
    ShippingOption,
    ZohoSyncJob,
)
from .tasks import (
    canonical_order_type,
    reconcile_pending_zoho_syncs,
    sync_order_to_zoho_task,
)
from .views.orders import CreateQuoteRequestView
from .views.stripe import _handle_fbi_payment
from .services.whatconverts_zoho import sync_phone_lead_to_zoho
from .zoho_sync import create_record_idempotently, get_order_external_key


class FakeResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data


class ZohoIdempotencyTests(TestCase):
    @patch('orders.zoho_sync._request')
    def test_duplicate_create_resolves_with_minimal_trigger_free_upsert(self, request):
        request.side_effect = [
            FakeResponse(
                400,
                {
                    'data': [
                        {
                            'code': 'DUPLICATE_DATA',
                            'status': 'error',
                            'message': 'duplicate data',
                        }
                    ]
                },
            ),
            FakeResponse(
                200,
                {
                    'data': [
                        {
                            'code': 'SUCCESS',
                            'status': 'success',
                            'details': {'id': 'zoho-123'},
                        }
                    ]
                },
            ),
        ]

        record_id = create_record_idempotently(
            module_name='Deals',
            record={
                'Deal_Name': 'FBI Standard ID42',
                'External_Order_Key': 'dcmn:fbi:42',
            },
            unique_field='External_Order_Key',
            unique_value='dcmn:fbi:42',
        )

        self.assertEqual(record_id, 'zoho-123')
        retry_payload = request.call_args_list[1].kwargs['json']
        self.assertEqual(
            retry_payload['data'],
            [{'External_Order_Key': 'dcmn:fbi:42'}],
        )
        self.assertEqual(retry_payload['trigger'], [])

    def test_external_key_and_i9_alias_are_stable(self):
        order = QuoteRequest.objects.create(
            name='Client',
            email='client@example.com',
            phone='2025550100',
            address='DC',
            number='1',
            appointment_date='2026-08-01',
            appointment_time='10:00 AM',
            services='Apostille Service',
            comments='',
        )
        self.assertEqual(get_order_external_key(order), f'dcmn:quote:{order.id}')
        self.assertEqual(canonical_order_type('I-9'), 'i9')

    def test_zoho_task_uses_late_ack_and_requeues_worker_loss(self):
        self.assertTrue(sync_order_to_zoho_task.acks_late)
        self.assertTrue(sync_order_to_zoho_task.reject_on_worker_lost)


class DurableQueueTests(TestCase):
    def setUp(self):
        self.package = FbiServicePackage.objects.create(
            code='standard',
            label='Standard',
            price=Decimal('100.00'),
        )
        self.shipping = ShippingOption.objects.create(
            code='ground',
            label='Ground',
            price=Decimal('10.00'),
        )

    def create_fbi(self, **overrides):
        values = {
            'name': 'Paid Client',
            'email': 'paid@example.com',
            'phone': '2025550101',
            'country_name': 'France',
            'address': 'Washington, DC',
            'comments': '',
            'package': self.package,
            'count': 1,
            'shipping_option': self.shipping,
            'total_price': Decimal('110.00'),
            'is_paid': True,
            'manager_notified': True,
        }
        values.update(overrides)
        return FbiApostilleOrder.objects.create(**values)

    @patch('orders.views.stripe.enqueue_zoho_sync')
    def test_repeated_stripe_webhook_repairs_paid_unsynced_order(self, enqueue):
        order = self.create_fbi(is_paid=True, zoho_synced=False)
        request = RequestFactory().post('/api/webhook/stripe/')

        _handle_fbi_payment(request, order.id, 'TRACK123')

        enqueue.assert_called_once_with(order.id, 'fbi', tracking_id='TRACK123')

    @patch('orders.tasks.sync_order_to_zoho_task.delay')
    def test_reconciler_republishes_pending_outbox_only(self, delay):
        paid = self.create_fbi(email='paid2@example.com', is_paid=True)
        self.create_fbi(email='unpaid@example.com', is_paid=False)
        ZohoSyncJob.objects.update_or_create(
            order_type='fbi',
            order_id=paid.id,
            defaults={'status': ZohoSyncJob.STATUS_PENDING},
        )
        ZohoSyncJob.objects.filter(
            order_type='fbi',
            order_id=paid.id,
        ).update(created_at=timezone.now() - timedelta(minutes=3))

        reconcile_pending_zoho_syncs.run()

        calls = {(call.args[0], call.args[1]) for call in delay.call_args_list}
        self.assertIn((paid.id, 'fbi'), calls)
        self.assertEqual(len(calls), 1)

    @patch('orders.tasks.sync_order_to_zoho_task.delay')
    def test_reconciler_skips_suppressed_duplicate(self, delay):
        order = self.create_fbi(email='duplicate@example.com')
        ZohoSyncJob.objects.update_or_create(
            order_type='fbi',
            order_id=order.id,
            defaults={
                'status': ZohoSyncJob.STATUS_SUPPRESSED,
                'last_error': 'Duplicate submission',
            },
        )

        reconcile_pending_zoho_syncs.run()

        delay.assert_not_called()


class QuoteDeduplicationTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.payload = {
            'name': 'Duplicate Client',
            'email': 'duplicate@example.com',
            'phone': '2025550199',
            'address': 'Washington, DC',
            'number': '1',
            'appointment_date': '2026-08-02',
            'appointment_time': '11:00 AM',
            'services': 'Apostille',
            'comments': 'Same request',
        }

    @patch('orders.views.orders.process_attribution')
    @patch('orders.views.orders.enqueue_zoho_sync')
    @patch('orders.services.notifications.build_order_extra_body', return_value='')
    @patch('orders.services.notifications.send_staff_notification')
    def test_duplicate_burst_reuses_order_but_later_request_is_allowed(
        self,
        notify,
        build_body,
        enqueue,
        process_attribution,
    ):
        view = CreateQuoteRequestView.as_view()

        first = view(
            self.factory.post('/api/orders/quote/', self.payload, format='json')
        )
        second = view(
            self.factory.post('/api/orders/quote/', self.payload, format='json')
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data['order_id'], second.data['order_id'])
        self.assertEqual(QuoteRequest.objects.count(), 1)
        self.assertTrue(
            ZohoSyncJob.objects.filter(
                order_type='quote',
                order_id=first.data['order_id'],
                status=ZohoSyncJob.STATUS_PENDING,
            ).exists()
        )
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(process_attribution.call_count, 1)
        self.assertEqual(notify.call_count, 1)

        QuoteRequestDeduplication.objects.update(
            last_accepted_at=timezone.now() - timedelta(seconds=31)
        )
        third = view(
            self.factory.post('/api/orders/quote/', self.payload, format='json')
        )

        self.assertEqual(third.status_code, 201)
        self.assertNotEqual(first.data['order_id'], third.data['order_id'])
        self.assertEqual(QuoteRequest.objects.count(), 2)
        self.assertEqual(enqueue.call_count, 2)
        self.assertEqual(process_attribution.call_count, 2)
        self.assertEqual(notify.call_count, 2)


class PhoneLeadReliabilityTests(TestCase):
    def create_phone_lead(self, **overrides):
        values = {
            'whatconverts_lead_id': 'wc-1001',
            'contact_name': 'Phone Client',
            'contact_email': 'phone@example.com',
            'contact_phone': '2025550188',
            'detected_service': 'fbi',
            'zoho_module': 'Deals',
            'raw_webhook_data': {},
        }
        values.update(overrides)
        return PhoneCallLead.objects.create(**values)

    @patch('orders.zoho_sync.get_or_create_contact_id', return_value=None)
    @patch('orders.zoho_sync.update_record_fields', return_value=True)
    @patch('orders.zoho_sync.create_attribution_record', return_value='attr-1')
    @patch('orders.zoho_sync.create_record_idempotently', return_value='lead-1')
    def test_phone_retry_reuses_primary_and_attribution_records(
        self,
        create_record,
        create_attribution,
        update_record,
        get_contact,
    ):
        phone_lead = self.create_phone_lead()

        self.assertTrue(sync_phone_lead_to_zoho(phone_lead))
        phone_lead.refresh_from_db()
        self.assertTrue(sync_phone_lead_to_zoho(phone_lead))

        self.assertEqual(create_record.call_count, 1)
        self.assertEqual(create_attribution.call_count, 1)
        phone_lead.refresh_from_db()
        self.assertTrue(phone_lead.zoho_synced)
        self.assertEqual(phone_lead.zoho_lead_id, 'lead-1')
        self.assertEqual(phone_lead.zoho_attribution_id, 'attr-1')

    @patch('orders.services.whatconverts_zoho.sync_phone_lead_to_zoho')
    def test_matched_phone_without_remote_id_still_finishes_idempotent_sync(
        self,
        sync_phone,
    ):
        phone_lead = self.create_phone_lead(
            whatconverts_lead_id='wc-1002',
            matched_with_form=True,
            matched_order_type='fbi',
            matched_order_id=999,
        )

        def complete_sync(instance):
            instance.zoho_module = 'Deals'
            instance.zoho_lead_id = 'lead-2'
            instance.zoho_attribution_id = 'attr-2'
            instance.zoho_synced = True
            instance.save(
                update_fields=[
                    'zoho_module',
                    'zoho_lead_id',
                    'zoho_attribution_id',
                    'zoho_synced',
                    'updated_at',
                ]
            )
            return True

        sync_phone.side_effect = complete_sync
        ZohoSyncJob.objects.update_or_create(
            order_type='phone',
            order_id=phone_lead.id,
            defaults={'status': ZohoSyncJob.STATUS_PENDING},
        )

        sync_order_to_zoho_task.run(phone_lead.id, 'phone')

        job = ZohoSyncJob.objects.get(order_type='phone', order_id=phone_lead.id)
        self.assertEqual(job.status, ZohoSyncJob.STATUS_SYNCED)
        self.assertEqual(job.zoho_record_id, 'lead-2')
