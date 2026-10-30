"""Independent speed history exercised through the real local HTTP query API."""
import http.client
import json
import os
import tempfile
import threading
import unittest

from token_ledger.api.server import create_server
from token_ledger.store import open_database


class SpeedHistoryHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'ledger.db')
        self.server = create_server(db_path=self.db, host='127.0.0.1', port=0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, headers, payload = self.request('GET', '/api/v1/bootstrap')
        self.assertEqual(status, 200)
        self.auth = {'Origin': f'http://127.0.0.1:{self.port}',
                     'Cookie': headers['Set-Cookie'].split(';')[0],
                     'X-CSRF-Token': payload['csrf_token']}

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), json.loads(response.read())
        conn.close()
        return result

    def test_history_identity_sparse_null_and_drilldown(self):
        conn = open_database(self.db, run_migrations=False)
        with conn:
            for sid, harness in [('s1', 'omp'), ('s2', 'claude-code')]:
                conn.execute("INSERT INTO sessions (session_id, origin_harness, account_alias, started_at, created_at) VALUES (?, ?, 'work', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')", (sid, harness))
            def add(ident, date, route='direct', harness='omp', output=100, duration=1000, ttft=300, fresh=100, cached=0):
                sid = 's1' if harness == 'omp' else 's2'
                conn.execute("INSERT INTO canonical_requests (canonical_id, provider, account_alias, origin_harness, access_route, raw_model, normalized_model, session_id, request_timestamp, timing_basis, generation_duration_ms, ttft_ms, created_at) VALUES (?, 'example', 'work', ?, ?, 'model', 'model', ?, ?, 'measured', ?, ?, ?)", (ident, harness, route, sid, date, duration, ttft, date))
                conn.execute('INSERT INTO normalized_usage (canonical_id, fresh_input_tokens, cache_read_tokens, output_tokens) VALUES (?, ?, ?, ?)', (ident, fresh, cached, output))
            for i in range(5):
                add(f'history-{i}', f'2026-10-{i+1:02}T10:00:00Z')
            for i in range(4):
                add(f'other-route-{i}', f'2026-10-{i+1:02}T10:00:00Z', route='relay')
            for i in range(5):
                add(f'other-harness-{i}', f'2026-10-{i+1:02}T10:00:00Z', harness='claude-code', output=1000)
            add('too-old', '2026-09-01T10:00:00Z', output=1000)
            add('slow', '2026-10-20T10:00:00Z', output=20, duration=2000, ttft=3500)
            add('sparse', '2026-10-20T11:00:00Z', route='relay', output=20, duration=2000, ttft=3500)
            add('zero', '2026-10-21T10:00:00Z', output=0, ttft=0)
            add('missing', '2026-10-22T10:00:00Z', output=None, ttft=None, cached=None)
        conn.close()
        query = {'contract_version': '1', 'metric_id': 'speed', 'harness_scope': 'omp',
                 'time_range': {'start': '2026-10-20T00:00:00Z', 'end': '2026-10-23T00:00:00Z', 'timezone': 'UTC'},
                 'group_by': ['day'], 'filters': {'model': ['model'], 'provider': ['example']}}
        status, _, result = self.request('POST', '/api/v1/query', query, self.auth)
        self.assertEqual(status, 200)
        self.assertEqual({(a['canonical_id'], a['anomaly_type']) for a in result['anomalies']}, {('slow', 'severe_tps_drop'), ('slow', 'ttft_spike')})
        self.assertEqual(result['anomaly_baseline']['window']['start'], '2026-09-20T00:00:00Z')
        groups = {g['access_route']: g for g in result['anomaly_baseline']['groups']}
        self.assertEqual((groups['direct']['tps_samples'], groups['direct']['median_tps']), (5, 100))
        self.assertEqual(groups['relay']['tps_samples'], 4)
        self.assertEqual(result['eligible']['eligible_zero_output_requests'], 1)
        self.assertEqual(result['eligible']['timed_generation_requests'], 3)
        self.assertEqual(result['timeline']['buckets'][-1]['weighted_tps'], None)
        self.assertEqual({p['canonical_id'] for p in result['points']}, {'slow', 'sparse', 'zero'})
        self.assertEqual(next(p for p in result['points'] if p['canonical_id'] == 'zero')['tps'], 0)
        narrowed = {**query, 'filters': {**query['filters'], 'route': ['relay']}}
        self.assertEqual(self.request('POST', '/api/v1/query', narrowed, self.auth)[2]['anomalies'], [])
        drill = {**query, 'metric_id': 'requests', 'search': 'slow', 'filters': {'session': ['s1']}}
        rows = self.request('POST', '/api/v1/query', drill, self.auth)[2]['rows']
        self.assertEqual([row['canonical_id'] for row in rows], ['slow'])

    def test_task_model_gates_independent_ttft_and_timeline_gap(self):
        conn = open_database(self.db, run_migrations=False)
        with conn:
            conn.execute("INSERT INTO sessions (session_id, origin_harness, created_at) VALUES ('s', 'omp', '2026-10-01T00:00:00Z')")
            for model, count in [('dense', 5), ('sparse', 4)]:
                for i in range(count):
                    ident = f'{model}-{i}'
                    date = '2026-10-20T10:00:00Z' if i < count - 1 else '2026-10-23T10:00:00Z'
                    conn.execute("INSERT INTO canonical_requests (canonical_id, provider, origin_harness, access_route, raw_model, normalized_model, session_id, request_timestamp, timing_basis, generation_duration_ms, ttft_ms, created_at) VALUES (?, 'example', 'omp', 'direct', ?, ?, 's', ?, 'measured', 1000, ?, ?)", (ident, model, model, date, 0 if i == 0 else (None if model == 'dense' and i == 1 else 100), date))
                    conn.execute("INSERT INTO normalized_usage (canonical_id, fresh_input_tokens, cache_read_tokens, output_tokens) VALUES (?, 0, ?, 100)", (ident, None if i == 1 else 0))
            conn.execute("INSERT INTO canonical_requests (canonical_id, provider, origin_harness, raw_model, normalized_model, session_id, request_timestamp, timing_basis, ttft_ms, created_at) VALUES ('ttft-only', 'example', 'omp', 'dense', 'dense', 's', '2026-10-20T11:00:00Z', 'measured', 0, '2026-10-20T11:00:00Z')")
            conn.execute("INSERT INTO normalized_usage (canonical_id, fresh_input_tokens, cache_read_tokens) VALUES ('ttft-only', 0, 0)")
            conn.execute("INSERT INTO manual_settings_and_outcomes (outcome_id, entity_type, entity_id, task_label, is_inferred, created_at, updated_at) VALUES ('label', 'session', 's', 'debugging', 1, '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')")
        conn.close()
        query = {'contract_version': '1', 'metric_id': 'speed', 'harness_scope': 'omp',
                 'time_range': {'start': '2026-10-20T00:00:00Z', 'end': '2026-10-24T00:00:00Z', 'timezone': 'UTC'},
                 'group_by': ['day']}
        status, _, result = self.request('POST', '/api/v1/query', query, self.auth)
        self.assertEqual(status, 200)
        dense, sparse = result['task_models']['debugging']['dense'], result['task_models']['debugging']['sparse']
        self.assertEqual((dense['comparison_status'], dense['ttft_comparison_status']), ('eligible', 'eligible'))
        self.assertEqual((dense['counts']['eligible_generation_requests'], dense['counts']['eligible_ttft_requests'], dense['counts']['total_requests']), (5, 5, 6))
        self.assertEqual((sparse['comparison_status'], sparse['ttft_comparison_status']), ('insufficient_samples', 'insufficient_samples'))
        self.assertEqual(dense['inferred_label_count'], 6)
        self.assertEqual((dense['tps']['p90'], dense['distributions']['ttft_ms']['p95']), (100, 100))
        points = {p['canonical_id']: p for p in result['points']}
        self.assertEqual((points['ttft-only']['tps'], points['ttft-only']['ttft_ms'], points['ttft-only']['input_volume_proxy']), (None, 0, 0))
        self.assertIsNone(points['dense-1']['input_volume_proxy'])
        gap = result['timeline']['buckets'][1]
        self.assertEqual((gap['start'], gap['end'], gap['weighted_tps'], gap['total_requests']), ('2026-10-21T00:00:00+00:00', '2026-10-23T00:00:00+00:00', None, 0))
