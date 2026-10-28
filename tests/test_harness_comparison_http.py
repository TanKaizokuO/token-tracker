"""Synthetic harness comparison through the local HTTP query contract."""
import http.client
import json
import os
import tempfile
import threading
import unittest

from token_ledger.api.server import create_server
from token_ledger.store import open_database


class HarnessComparisonHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'ledger.db')
        self.server = create_server(db_path=self.db, host='127.0.0.1', port=0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _, headers, bootstrap = self.call('GET', '/api/v1/bootstrap')
        self.auth = {'Origin': f'http://127.0.0.1:{self.port}', 'Cookie': headers['Set-Cookie'].split(';')[0], 'X-CSRF-Token': bootstrap['csrf_token']}
        conn = open_database(self.db, run_migrations=False)
        with conn:
            conn.execute("INSERT INTO sessions (session_id, origin_harness, started_at, status, duration_ms, created_at) VALUES ('c', 'claude-code', '2026-10-01T09:00:00Z', 'complete', 60000, '2026-10-01T00:00:00Z'), ('o', 'oh-my-pi', '2026-10-02T09:00:00Z', 'complete', NULL, '2026-10-01T00:00:00Z')")
            for ident, harness, session, model, date, basis, gen, ttft in [
                ('c1', 'claude-code', 'c', 'alpha', '2026-10-01T10:00:00Z', 'measured', 1000, 100),
                ('c2', 'claude-code', 'c', 'beta', '2026-10-02T10:00:00Z', 'none', None, None),
                ('o1', 'oh-my-pi', 'o', 'alpha', '2026-10-02T11:00:00Z', 'none', None, None),
            ]:
                conn.execute("INSERT INTO canonical_requests (canonical_id, provider, account_alias, origin_harness, access_route, raw_model, normalized_model, session_id, request_timestamp, timing_basis, generation_duration_ms, ttft_ms, created_at) VALUES (?, 'example', 'work', ?, 'direct', ?, ?, ?, ?, ?, ?, ?, ?)", (ident, harness, model, model, session, date, basis, gen, ttft, date))
            conn.execute("UPDATE canonical_requests SET access_route = 'gateway' WHERE canonical_id = 'c2'")
            conn.execute("""INSERT INTO events (event_id, session_id, canonical_id, event_kind, event_name, started_at, ended_at, status, details_json) VALUES
                ('e1', 'c', 'c1', 'request', 'request', '2026-10-01T10:00:00Z', '2026-10-01T10:10:00Z', 'ok', '{}'),
                ('e2', 'c', 'c1', 'request', 'request', '2026-10-01T10:05:00Z', '2026-10-01T10:15:00Z', 'ok', '{}')""")
            conn.execute("INSERT INTO canonical_requests (canonical_id, provider, account_alias, origin_harness, access_route, raw_model, normalized_model, session_id, request_timestamp, timing_basis, created_at) VALUES ('prior', 'example', 'work', 'claude-code', 'direct', 'alpha', 'alpha', 'c', '2026-09-30T10:00:00Z', 'none', '2026-09-30T10:00:00Z')")
            conn.execute("INSERT INTO normalized_usage (canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens, reasoning_tokens, reported_total_tokens) VALUES ('c1', 100, 50, 10, 0, 0, 20, 5, 180), ('c2', 0, 0, 0, 0, 0, 10, 0, 10), ('o1', 40, NULL, NULL, 0, 7, 8, NULL, 55)")
            conn.execute("INSERT INTO financial_facts (canonical_id, api_equivalent_usd, cache_savings_usd, pricing_status, calculated_at) VALUES ('c1', 0.5, 0.1, 'priced', '2026-10-01T10:00:00Z'), ('c2', 0, 0, 'priced', '2026-10-02T10:00:00Z'), ('o1', NULL, NULL, 'unpriced', '2026-10-02T11:00:00Z')")
            conn.execute("INSERT INTO manual_settings_and_outcomes (outcome_id, entity_type, entity_id, task_label, completion_rating, is_inferred, created_at, updated_at) VALUES ('done', 'session', 'c', 'coding', 'complete', 0, '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')")
            conn.execute("""INSERT INTO events (event_id, session_id, canonical_id, event_kind, event_name, duration_ms, output_size_bytes, status, details_json, started_at) VALUES
                ('tool-a', 'c', 'c1', 'tool', 'ReadOriginal', 12, 0, 'error', '{}', '2026-10-01T10:00:00Z'),
                ('tool-b', 'c', 'c2', 'tool', 'HiddenOtherModel', 25, 90, 'ok', '{}', '2026-10-02T10:00:00Z'),
                ('tool-old', 'c', 'prior', 'tool', 'HiddenPriorPeriod', 30, 100, 'ok', '{}', '2026-09-30T10:00:00Z'),
                ('standalone', 'c', NULL, 'tool', 'Standalone', 40, 50, 'ok', '{}', '2026-10-01T10:00:00Z'),
                ('agent-a', 'c', 'c1', 'subagent', 'Worker', NULL, NULL, 'ok', '{}', '2026-10-01T10:00:00Z')""")
            conn.execute("UPDATE canonical_requests SET request_status = 'error' WHERE canonical_id = 'c1'")
        conn.close()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        client.request(method, path, json.dumps(body) if body is not None else None, {'Content-Type': 'application/json', **(headers or {})})
        response = client.getresponse()
        result = response.status, dict(response.getheaders()), json.loads(response.read())
        client.close()
        return result

    def query(self, scope, metric, group=None, filters=None):
        envelope = {'contract_version': '1', 'harness_scope': scope, 'metric_id': metric, 'time_range': {'start': '2026-10-01T00:00:00Z', 'end': '2026-10-03T00:00:00Z', 'timezone': 'UTC'}, 'filters': filters or {'provider': ['example']}}
        if group:
            envelope['group_by'] = [group]
        status, _, result = self.call('POST', '/api/v1/query', envelope, self.auth)
        self.assertEqual(status, 200, result)
        return result

    def test_harness_models_days_coverage_timing_outcomes_and_drilldown(self):
        claude = self.query('claude-code', 'tokens', 'model')['rows']
        omp = self.query('omp', 'tokens', 'model')['rows']
        a = next(row for row in claude if row['group_key'] == 'alpha')
        self.assertEqual((a['requests_count'], a['sessions_count'], a['fresh_input_tokens'], a['output_tokens']), (1, 1, 100, 20))
        self.assertEqual((a['api_equivalent_usd'], a['api_equivalent_usd_status']), (0.5, 'exact'))
        self.assertEqual((omp[0]['cache_write_unknown_ttl_tokens'], omp[0]['cache_read_tokens'], omp[0]['token_fields_status']['cache_read_tokens']), (7, None, 'unavailable'))
        self.assertIsNone(omp[0]['api_equivalent_usd'])
        self.assertEqual(omp[0]['total_tokens_status'], 'partial')
        days = self.query('combined', 'tokens', 'day')['rows']
        self.assertEqual([(r['group_key'], r['requests_count']) for r in days], [('2026-10-01', 1), ('2026-10-02', 2)])
        summary = self.query('claude-code', 'summary')
        self.assertEqual((summary['values']['session_duration_ms'], summary['missingness']['session_duration_observed_count']), (60000, 1))
        self.assertIsNone(self.query('omp', 'summary')['values']['session_duration_ms'])
        speed = self.query('claude-code', 'speed')
        self.assertEqual((speed['eligible']['timed_generation_requests'], speed['eligible']['total_requests']), (1, 2))
        self.assertEqual(self.query('claude-code', 'outcomes')['values']['completed_tasks'], 1)
        self.assertEqual(self.query('omp', 'outcomes')['values']['completed_tasks'], 0)
        filters = {'provider': ['example'], 'model': ['alpha']}
        drill = self.query('omp', 'sessions', filters=filters)
        self.assertEqual([row['session_id'] for row in drill['rows']], ['o'])
        self.assertEqual(self.query('claude-code', 'tokens', 'model', filters)['rows'][0]['requests_count'], 1)

    def test_time_comparison_scope_distribution_and_interval_union(self):
        summary = self.query('claude-code', 'summary')
        dist = summary['session_duration_distribution']
        self.assertEqual((dist['basis'], dist['count'], dist['linked_sessions'], dist['p50'], dist['status']), ('stored_session_duration_ms', 1, 1, 60000, 'exact'))
        self.assertIsNone(self.query('omp', 'summary')['session_duration_distribution']['p50'])
        day = self.query('claude-code', 'timeline', 'day')['rows'][0]
        self.assertEqual((day['active_minutes'], day['active_minutes_timed_requests'], day['active_minutes_basis']), (15, 1, 'explicit_request_event_interval_union'))
        speed = self.query('claude-code', 'speed')
        self.assertEqual((speed['slices']['model']['beta']['counts']['total_requests'], speed['slices']['model']['beta']['counts']['primary_basis_requests']), (1, 0))
        self.assertIsNone(speed['slices']['route']['gateway']['tps']['weighted'])
        self.assertEqual(speed['slices']['route']['direct']['tps']['weighted'], 20)
        self.assertEqual(speed['slices']['model_route']['alpha']['direct']['tps']['weighted'], 20)
        self.assertIsNone(speed['slices']['model_route']['beta']['gateway']['tps']['weighted'])
        envelope = {'contract_version': '1', 'harness_scope': 'claude-code', 'metric_id': 'comparisons',
                    'time_range': {'start': '2026-10-01T00:00:00Z', 'end': '2026-10-03T00:00:00Z', 'timezone': 'UTC'},
                    'filters': {'provider': ['example'], 'model': ['alpha']},
                    'comparison_periods': {'period_a': {'start': '2026-09-29T00:00:00Z', 'end': '2026-10-01T00:00:00Z', 'timezone': 'UTC'},
                                           'period_b': {'start': '2026-10-01T00:00:00Z', 'end': '2026-10-03T00:00:00Z', 'timezone': 'UTC'}}}
        status, _, result = self.call('POST', '/api/v1/query', envelope, self.auth)
        self.assertEqual(status, 200, result)
        self.assertEqual((result['period_a']['metrics']['requests_count'], result['period_b']['metrics']['requests_count']), (1, 1))
        self.assertEqual(result['query']['filters']['model'], ['alpha'])

    def test_trace_scope_and_comparison_missingness(self):
        filters = {'provider': ['example'], 'model': ['alpha'], 'session': ['c']}
        trace = self.query('claude-code', 'traces', filters=filters)
        self.assertEqual([t['tool_name'] for t in trace['tools_breakdown']], ['ReadOriginal'])
        tool = trace['tools_breakdown'][0]
        self.assertEqual((tool['invocations_count'], tool['total_duration_ms'], tool['error_count'], tool['total_output_size_bytes']), (1, 12, 1, 0))
        self.assertEqual(trace['values']['subagent_calls_count'], 1)
        self.assertEqual(trace['retry_loops']['suspected_waste_signals'][0]['canonical_id'], 'c1')
        self.assertEqual(trace['event_scope'], 'filtered_requests')
        self.assertEqual([e['event_name'] for e in trace['session_level_events']], ['Standalone'])
        envelope = {'contract_version': '1', 'harness_scope': 'claude-code', 'metric_id': 'comparisons',
                    'time_range': {'start': '2026-10-01T00:00:00Z', 'end': '2026-10-03T00:00:00Z', 'timezone': 'UTC'},
                    'filters': filters,
                    'comparison_periods': {'period_a': {'start': '2026-09-29T00:00:00Z', 'end': '2026-10-01T00:00:00Z', 'timezone': 'UTC'},
                                           'period_b': {'start': '2026-10-01T00:00:00Z', 'end': '2026-10-03T00:00:00Z', 'timezone': 'UTC'}}}
        status, _, result = self.call('POST', '/api/v1/query', envelope, self.auth)
        self.assertEqual(status, 200, result)
        a, b = result['period_a']['metrics'], result['period_b']['metrics']
        self.assertIsNone(a['total_tokens'])
        self.assertIsNone(a['api_equivalent_usd'])
        self.assertEqual((a['coverage']['total_tokens'], a['coverage']['api_equivalent_usd']), ('unavailable', 'unavailable'))
        self.assertEqual((b['total_tokens'], b['coverage']['total_tokens'], b['api_equivalent_usd']), (180, 'exact', 0.5))
        self.assertEqual(b['outcomes']['completed_tasks'], 1)
        envelope['filters'] = {'provider': ['example']}
        envelope['harness_scope'] = 'combined'
        _, _, result = self.call('POST', '/api/v1/query', envelope, self.auth)
        b = result['period_b']['metrics']
        self.assertEqual((b['api_equivalent_usd'], b['coverage']['api_equivalent_usd']), (0.5, 'partial'))
        self.assertEqual((b['total_tokens'], b['coverage']['total_tokens']), (245, 'partial'))

    def test_request_reliability_and_incomplete_completed_timing(self):
        conn = open_database(self.db, run_migrations=False)
        with conn:
            conn.execute("UPDATE canonical_requests SET normalized_model='alpha', raw_model='alpha' WHERE canonical_id='c2'")
            conn.execute("UPDATE canonical_requests SET request_status='ok', duration_ms=1000 WHERE canonical_id='c1'")
            conn.execute("UPDATE canonical_requests SET request_status='unknown', duration_ms=NULL WHERE canonical_id='c2'")
            for index, status in enumerate(['error', 'rate_limited', 'timeout', 'interrupted']):
                conn.execute("""INSERT INTO canonical_requests
                    (canonical_id, provider, origin_harness, access_route, raw_model, normalized_model,
                     session_id, request_timestamp, request_status, created_at)
                    VALUES (?, 'example', 'claude-code', 'direct', 'alpha', 'alpha', 'c',
                    '2026-10-01T12:00:00Z', ?, '2026-10-01T12:00:00Z')""", (f'status-{index}', status))
            for index in range(2):
                sid = f'complete-{index}'
                conn.execute("INSERT INTO sessions (session_id, origin_harness, created_at) VALUES (?, 'claude-code', '2026-10-01T00:00:00Z')", (sid,))
                conn.execute("""INSERT INTO canonical_requests (canonical_id, provider, origin_harness,
                    access_route, raw_model, normalized_model, session_id, request_timestamp, request_status, duration_ms, created_at)
                    VALUES (?, 'example', 'claude-code', 'direct', 'alpha', 'alpha', ?,
                    '2026-10-01T13:00:00Z', 'ok', 1000, '2026-10-01T13:00:00Z')""", (sid, sid))
                conn.execute("""INSERT INTO manual_settings_and_outcomes
                    (outcome_id, entity_type, entity_id, task_label, completion_rating, created_at, updated_at)
                    VALUES (?, 'session', ?, 'coding', 'complete', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')""", (sid, sid))
        conn.close()
        result = self.query('claude-code', 'outcomes')
        reliability = result['request_reliability']
        self.assertEqual((reliability['eligible_requests'], reliability['status_covered_requests'], reliability['unknown_status_requests']), (8, 7, 1))
        for status in ['error', 'rate_limited', 'timeout', 'interrupted']:
            self.assertEqual(reliability['statuses'][status], {'count': 1, 'rate': 1 / 7})
        self.assertEqual(reliability['failed_requests'], {'count': 4, 'rate': 4 / 7})
        self.assertTrue(result['values']['adequate_sample'])
        self.assertIsNone(result['values']['time_per_completed_task_seconds'])
        self.assertEqual(result['values']['timed_completed_tasks'], 2)
        self.assertIsNone(result['values']['cost_per_success_usd'])
        # Tool errors are not request errors (c1 has an error tool but explicit ok status).
        self.assertEqual(reliability['statuses']['ok']['count'], 3)
        models = self.query('claude-code', 'models')['rows']
        self.assertEqual({r['access_route'] for r in models if r['model'] == 'alpha'}, {'direct', 'gateway'})


if __name__ == '__main__':
    unittest.main()
