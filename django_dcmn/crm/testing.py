"""Shared test base for the quotes and crm apps (DESIGN §14): no network, ever.

`NoNetworkTestCase` patches `requests.Session.request`, which sits below `requests.request`/`requests.post`
and every transport (legacy or new), with `HttpSpy`. The spy records each call and raises `NetworkBlocked`
unless the test queued a response or installed a handler. It also blanks the Zoho and Turnstile credentials
that settings_test inherits from `.env`, and clears the cache before each test.

    class MyTests(NoNetworkTestCase):
        def test_x(self):
            self.warm_zoho_token()
            self.http.queue(FakeResponse(201, {'data': [{'code': 'SUCCESS', 'details': {'id': '1'}}]}))
            ...
            self.assertEqual(self.http.calls[0].json['trigger'], [])
"""

import itertools
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings

ZOHO_TEST_TOKEN = 'test-token'
_references = itertools.count(100001)


class NetworkBlocked(Exception):
    """A test made an HTTP call it did not prepare for."""


class FakeResponse:
    """The subset of requests.Response the code under test reads (cf. orders/test_zoho_reliability.py)."""

    def __init__(self, status_code: int = 200, data: Any = None, headers: dict | None = None):
        self.status_code = status_code
        self._data = data
        self.headers = headers or {}
        self.text = '' if data is None else str(data)
        self.content = self.text.encode('utf-8')
        self.ok = status_code < 400

    def json(self):
        if self._data is None:
            raise ValueError('no JSON body')
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f'HTTP {self.status_code}', response=self)


@dataclass
class HttpCall:
    method: str
    url: str
    params: dict | None
    json: Any
    timeout: Any
    headers: dict | None


class HttpSpy:
    """Stands in for requests.Session.request: records calls, answers from a queue or a handler."""

    def __init__(self):
        self.calls: list[HttpCall] = []
        self.handler: Callable[[HttpCall], FakeResponse] | None = None
        self._queue: deque = deque()

    def queue(self, *responses) -> None:
        """Answer the next calls in order; an exception instance is raised instead of returned."""
        self._queue.extend(responses)

    def __call__(self, method: str, url: str, **kwargs) -> FakeResponse:
        call = HttpCall(
            method=str(method).upper(), url=url, params=kwargs.get('params'), json=kwargs.get('json'),
            timeout=kwargs.get('timeout'), headers=kwargs.get('headers'),
        )
        self.calls.append(call)
        if self._queue:
            result = self._queue.popleft()
        elif self.handler is not None:
            result = self.handler(call)
        else:
            raise NetworkBlocked(f'unexpected HTTP call: {call.method} {call.url}')
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def writes(self) -> list[HttpCall]:
        return [c for c in self.calls if c.method in ('POST', 'PUT', 'PATCH', 'DELETE')]


@override_settings(ZOHO_REFRESH_TOKEN='', ZOHO_CLIENT_ID='', ZOHO_CLIENT_SECRET='', TURNSTILE_SECRET_KEY='')
class NoNetworkTestCase(TestCase):
    """Base class of every quotes and crm test case."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.http = HttpSpy()
        spy = self.http

        def session_request(session, method, url, **kwargs):
            return spy(method, url, **kwargs)

        patcher = patch('requests.Session.request', new=session_request)
        patcher.start()
        self.addCleanup(patcher.stop)

    def warm_zoho_token(self) -> None:
        """Seed the legacy token cache so no OAuth refresh runs."""
        cache.set('zoho_access_token', ZOHO_TEST_TOKEN, 2900)

    def assertNoHttp(self) -> None:
        self.assertEqual(self.http.calls, [], 'unexpected HTTP calls')


def make_quote_submission(**fields):
    """A saved QuoteSubmission with test contact data; `fields` override the defaults."""
    from quotes.models import QuoteSubmission

    values = {
        'reference': f'Q-{next(_references)}',
        'submission_id': uuid.uuid4(),
        'schema_version': 'quote.v5',
        'form_version': 'test',
        'is_test': True,
        'full_name': 'ZZ TEST Jane Doe',
        'email': 'jane.doe@example.com',
        'email_normalized': 'jane.doe@example.com',
        'phone_raw': '(202) 555-0123',
        'phone_e164': '+12025550123',
        'phone_last10': '2025550123',
        'phone_valid': True,
        'payload_core_hash': '0' * 64,
    }
    values.update(fields)
    return QuoteSubmission.objects.create(**values)
