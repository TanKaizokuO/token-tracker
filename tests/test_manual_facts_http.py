"""Synthetic HTTP coverage for manual outcomes and independent fee facts."""
import http.client
import json
import os
import tempfile
import threading
import unittest

from token_ledger.api.server import create_server
from token_ledger.store.database import open_database


class ManualFactsHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'ledger.db')
        self.server = create_server(self.db, host='127.0.0.1', port=0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        conn = open_database(self.db)
        with conn:
            conn.execute("INSERT INTO sessions (session_id, origin_harness, started_at, created_at) VALUES ('session-synthetic', 'omp', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
            conn.execute("INSERT INTO canonical_requests (canonical_id, provider, account_alias, origin_harness, access_route, raw_model, normalized_model, session_id, request_timestamp, created_at) VALUES ('request-synthetic', 'example', 'acct', 'omp', 'direct', 'model', 'model', 'session-synthetic', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
            conn.execute("INSERT INTO normalized_usage (canonical_id, fresh_input_tokens, output_tokens) VALUES ('request-synthetic', 100, 10)")
            conn.execute("INSERT INTO financial_facts (canonical_id, actual_billed_amount, actual_billed_currency, api_equivalent_usd, calculated_at) VALUES ('request-synthetic', 7, 'USD', 5, '2026-01-01T00:00:00Z')")
        conn.close()
        _, headers, bootstrap = self.call('GET', '/api/v1/bootstrap')
        self.headers = {'Origin': f'http://127.0.0.1:{self.port}', 'Cookie': headers['Set-Cookie'].split(';')[0], 'X-CSRF-Token': bootstrap['csrf_token']}

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.port)
        request_headers = {'Host': f'127.0.0.1:{self.port}', **(headers or {})}
        if body is not None:
            body = json.dumps(body); request_headers['Content-Type'] = 'application/json'
        client.request(method, path, body, request_headers)
        response = client.getresponse(); data = json.loads(response.read()); result = (response.status, dict(response.getheaders()), data)
        client.close(); return result

    def test_fee_is_independent_and_requires_same_origin(self):
        fee = {'provider': 'example', 'account_alias': 'acct', 'access_route': 'direct', 'paid_date': '2026-01-03', 'amount': 0, 'currency': 'USD', 'source': 'synthetic invoice'}
        empty_fee_list = self.call('GET', '/api/v1/subscription-fees')[2]
        self.assertEqual(empty_fee_list['total'], 0)
        self.assertEqual(empty_fee_list['subscription_fees'], [])
        self.assertEqual(self.call('POST', '/api/v1/subscription-fees', fee)[0], 403)
        status, _, created = self.call('POST', '/api/v1/subscription-fees', fee, self.headers)
        zero_fee_list = self.call('GET', '/api/v1/subscription-fees')[2]
        self.assertEqual(zero_fee_list['total'], 1)
        self.assertEqual(zero_fee_list['subscription_fees'][0]['amount'], 0)
        self.assertEqual(status, 200); self.assertEqual(created['amount'], 0)
        path = '/api/v1/subscription-fees/' + created['fee_id']
        self.assertEqual(self.call('PATCH', path, {**fee, 'amount': -1}, self.headers)[0], 400)
        self.assertEqual(self.call('PATCH', path, {**fee, 'amount': 12.5, 'currency': 'EUR'}, self.headers)[0], 200)
        self.assertEqual(self.call('GET', '/api/v1/subscription-fees')[2]['subscription_fees'][0]['amount'], 12.5)
        other = {**fee, 'paid_date': '2026-01-04', 'amount': 0}
        self.assertEqual(self.call('POST', '/api/v1/subscription-fees', other, self.headers)[0], 200)
        page1 = self.call('GET', '/api/v1/subscription-fees?limit=1&offset=0')[2]
        page2 = self.call('GET', '/api/v1/subscription-fees?limit=1&offset=1')[2]
        self.assertEqual((page1['total'], page1['offset'], page1['subscription_fees'][0]['amount']), (2, 0, 0))
        self.assertEqual((page2['total'], page2['offset'], page2['subscription_fees'][0]['amount']), (2, 1, 12.5))
        self.assertEqual(self.call('GET', '/api/v1/subscription-fees?limit=201')[0], 400)
        self.assertEqual(self.call('GET', '/api/v1/subscription-fees?offset=-1')[0], 400)
        query = {'contract_version': '1', 'harness_scope': 'combined', 'metric_id': 'requests'}
        rows = self.call('POST', '/api/v1/query', query, self.headers)[2]['rows']
        self.assertEqual(rows[0]['api_equivalent_usd'], 5)
        conn = open_database(self.db)
        fact = conn.execute("SELECT actual_billed_amount, actual_billed_currency FROM financial_facts WHERE canonical_id='request-synthetic'").fetchone()
        self.assertEqual(tuple(fact), (7, 'USD')); conn.close()

    def test_session_outcome_create_edit_and_shared_query(self):
        outcome = {'entity_type': 'session', 'entity_id': 'session-synthetic', 'account_alias': 'acct', 'task_label': 'debugging', 'completion_rating': 'unsuccessful'}
        status, _, created = self.call('POST', '/api/v1/outcomes', outcome, self.headers)
        self.assertEqual(status, 200); self.assertIsNone(created['manual_rating']); self.assertFalse(created['is_inferred'])
        self.assertEqual(self.call('PATCH', '/api/v1/outcomes/' + created['outcome_id'], {'completion_rating': 'complete', 'manual_rating': 0}, self.headers)[0], 200)
        records = self.call('GET', '/api/v1/outcomes?entity_type=session&entity_id=session-synthetic')[2]['outcomes']
        self.assertEqual(records[0]['manual_rating'], 0)
        query = {'contract_version': '1', 'harness_scope': 'combined', 'metric_id': 'outcomes'}
        response = self.call('POST', '/api/v1/query', query, self.headers)[2]
        self.assertEqual(response['values']['completed_tasks'], 1)
        self.assertEqual(response['tasks'][0]['task_label'], 'debugging')
        self.assertEqual(response['values']['user_ratings_count'], 1)
        self.assertEqual(response['values']['user_ratings_mean'], 0)
        path = '/api/v1/outcomes/' + created['outcome_id']
        unchanged = self.call('PATCH', path, {'task_label': 'review'}, self.headers)[2]
        self.assertEqual(unchanged['manual_rating'], 0)
        cleared = self.call('PATCH', path, {'manual_rating': None}, self.headers)[2]
        self.assertIsNone(cleared['manual_rating'])
        reloaded = self.call('GET', '/api/v1/outcomes?entity_type=session&entity_id=session-synthetic')[2]['outcomes'][0]
        self.assertIsNone(reloaded['manual_rating'])
        response = self.call('POST', '/api/v1/query', query, self.headers)[2]
        self.assertEqual(response['values']['user_ratings_count'], 0)
        self.assertIsNone(response['values']['user_ratings_mean'])
