"""Model criteria consumer regression through the actual loopback HTTP API."""
import http.client
import json
import os
import tempfile
import threading
import unittest

from token_ledger.api.server import create_server
from token_ledger.store import open_database


def populate_models_fixture(path):
    """Synthetic measured-zero, unpriced, matched, sparse and missing observations."""
    conn = open_database(path)
    with conn:
        for model, raw, count in [('Sonnet', 'claude-sonnet-5-5', 5), ('Opus', 'claude-opus-5-5', 5), ('Haiku', 'claude-haiku-5-5', 1)]:
            sid = f'session-{model}'
            conn.execute("INSERT INTO sessions(session_id, origin_harness, created_at) VALUES (?, 'claude-code', '2026-10-25T00:00:00Z')", (sid,))
            for i in range(count):
                cid = f'{model}-{i}'
                date = f'2026-10-25T10:0{i}:00Z'
                conn.execute("INSERT INTO canonical_requests(canonical_id, provider, origin_harness, access_route, raw_model, normalized_model, session_id, request_timestamp, timing_basis, duration_ms, generation_duration_ms, ttft_ms, request_status, created_at) VALUES (?, 'anthropic', 'claude-code', 'direct', ?, ?, ?, ?, 'measured', ?, 1000, ?, ?, ?)", (cid, raw, raw, sid, date, 1000 + i * 100, None if i == 0 else 100 + i, 'unknown' if i == 0 else 'ok', date))
                conn.execute('INSERT INTO normalized_usage(canonical_id, fresh_input_tokens, cache_read_tokens, output_tokens, reasoning_tokens) VALUES (?, 100, 0, ?, 0)', (cid, None if i == 4 else i * 10))
                if model == 'Sonnet':
                    conn.execute("INSERT INTO financial_facts(canonical_id, pricing_status, api_equivalent_usd, api_equivalent_inr, actual_billed_amount, calculated_at) VALUES (?, 'priced', 0, 0, 0, ?)", (cid, date))
                conn.execute("INSERT INTO manual_settings_and_outcomes(outcome_id, entity_type, entity_id, task_label, manual_rating, is_inferred, created_at, updated_at) VALUES (?, 'request', ?, 'review', 4, 0, ?, ?)", (f'outcome-{cid}', cid, date, date))
    conn.close()


class ModelsCriteriaHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'models.sqlite3')
        populate_models_fixture(self.db)
        self.server = create_server(db_path=self.db, host='127.0.0.1', port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _, headers, body = self.request('GET', '/api/v1/bootstrap')
        self.auth = {'Origin': f'http://127.0.0.1:{self.server.server_port}', 'Cookie': headers['Set-Cookie'].split(';')[0], 'X-CSRF-Token': body['csrf_token']}

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        conn.request(method, path, json.dumps(body) if body else None, getattr(self, 'auth', {}))
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), json.loads(response.read())
        conn.close()
        return result

    def test_zero_unpriced_distributions_matched_and_sparse(self):
        query = {'contract_version': '1', 'metric_id': 'model_profiles', 'harness_scope': 'combined'}
        status, _, result = self.request('POST', '/api/v1/query', query)
        self.assertEqual(status, 200)
        self.assertEqual(len(result['profiles']), 6)
        sonnet = result['profiles']['Sonnet 5.5']
        self.assertEqual(sonnet['cost']['api_equivalent_usd'], 0)
        self.assertEqual(sonnet['cost']['api_equivalent_inr'], 0)
        self.assertIsNone(result['profiles']['Opus 5.5']['cost']['api_equivalent_usd'])
        self.assertIsNone(result['profiles']['Sol 6.1']['cost']['api_equivalent_inr'])
        self.assertEqual(sonnet['session_mix'][0]['requests_count'], 5)
        self.assertEqual(sonnet['session_trends'][0]['requests_count'], 5)
        task = next(row for row in result['task_slices']['rows'] if row['model_identity'] == 'Sonnet 5.5')
        self.assertEqual((task['output_distribution']['count'], task['output_distribution']['p50'], task['output_distribution']['p95'], task['output_missing_count']), (4, 10, 30, 1))
        rows = result['comparison']['task_cohorts'][0]['models']
        sonnet_comp = next(row for row in rows if row['model_identity'] == 'Sonnet 5.5')
        metrics = sonnet_comp['metrics']
        self.assertEqual((metrics['median_output']['value'], metrics['median_output']['sample_count']), (10, 4))
        self.assertEqual((metrics['median_ttft_ms']['denominator'], metrics['retry_error_rate']['denominator']), (4, 4))
        self.assertEqual(metrics['p95_duration_ms']['value'], 1400)
        self.assertIsNone(metrics['outcome_rating']['value'])  # sparse third model blocks full cohort
        self.assertEqual(metrics['outcome_rating']['sample_count'], 5)
        self.assertEqual(next(row for row in rows if row['model_identity'] == 'Haiku 5.5')['metrics']['median_ttft_ms']['value'], None)
        # Same shared filter removes the sparse identity; actual cohort gate opens.
        query['filters'] = {'model': ['claude-sonnet-5-5', 'claude-opus-5-5']}
        status, _, narrowed = self.request('POST', '/api/v1/query', query)
        self.assertEqual(status, 200)
        cohort = narrowed['comparison']['task_cohorts'][0]
        self.assertTrue(cohort['adequate'])
        for row in cohort['models']:
            self.assertTrue(row['effectiveness_eligible'])
            self.assertEqual(row['metrics']['outcome_rating']['value'], 4)
            self.assertEqual(row['metrics']['outcome_rating']['denominator'], 5)
        self.assertEqual(narrowed['comparison']['matched_filters']['filters'], query['filters'])


if __name__ == '__main__':
    unittest.main()
