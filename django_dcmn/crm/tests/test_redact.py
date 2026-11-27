"""crm.redact: stored engine results keep field names and lengths only (WP1)."""

import copy
import json

from crm.models import EngineRequest
from crm.redact import payload_sha256, redact_result
from crm.testing import NoNetworkTestCase

PAYLOAD = {
    'Name': 'Jane Doe Apostille',
    'Email': 'jane.doe@example.com',
    'Phone': '+12025550123',
    'Quantity': 2,
    'Rush': False,
    'Notes': None,
    'Documents': ['birth certificate', 'diploma'],
}
BODY = {
    'ok': True,
    'job_id': '2f1f4c1e-8f0e-4d55-9f43-6c7a2b1d9e10',
    'services': [{
        'service_key': 'apostille',
        'state': 'preview',
        'payload': PAYLOAD,
        'candidates': [{
            'module': 'Deals', 'id': '5725767000012345678', 'name': 'Jane Doe', 'stage': 'New',
            'url': 'https://crm.zoho.com/crm/org882204370/tab/Potentials/5725767000012345678', 'match': 'email',
        }],
    }],
}


class RedactTests(NoNetworkTestCase):
    def test_payload_reduced_to_names_and_lengths(self):
        body = copy.deepcopy(BODY)

        request = EngineRequest.objects.create(caller='zoho', mode='preview', result=redact_result(body))
        request.refresh_from_db()

        stored = json.dumps(request.result)
        for value in ('jane.doe@example.com', '2025550123', 'Jane Doe', 'birth certificate', 'Potentials'):
            self.assertNotIn(value, stored)

        service = request.result['services'][0]
        self.assertEqual(service['payload'], {
            'Name': {'type': 'str', 'len': 18},
            'Email': {'type': 'str', 'len': 20},
            'Phone': {'type': 'str', 'len': 12},
            'Quantity': {'type': 'int', 'len': 1},
            'Rush': {'type': 'bool', 'len': 1},
            'Notes': {'type': 'null', 'len': 0},
            'Documents': {'type': 'list', 'len': 2},
        })
        self.assertEqual(service['payload_sha256'], payload_sha256(PAYLOAD))
        self.assertEqual(service['candidates'], [{'module': 'Deals', 'id': '5725767000012345678', 'match': 'email'}])
        self.assertEqual((service['service_key'], service['state']), ('apostille', 'preview'))
        self.assertEqual(body, BODY, 'the caller keeps the full body')

    def test_payload_hash_ignores_key_order(self):
        reordered = dict(reversed(list(PAYLOAD.items())))

        self.assertEqual(payload_sha256(reordered), payload_sha256(PAYLOAD))
        self.assertNotEqual(payload_sha256({**PAYLOAD, 'Quantity': 3}), payload_sha256(PAYLOAD))
