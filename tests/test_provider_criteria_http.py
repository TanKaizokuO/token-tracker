"""Provider criteria through native synthetic ingestion and real local HTTP."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from token_ledger.api.server import create_server
from token_ledger.ingest.runner import ingest_file
from token_ledger.metrics.providers import import_quota_snapshot
from token_ledger.store import open_database


class ProviderCriteriaHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'ledger.db')
        conn = open_database(self.db)
        now = datetime.now(timezone.utc)
        path = os.path.join(self.tmp.name, 'synthetic.jsonl')
        records = []
        for i in range(7):
            timestamp = (now - timedelta(minutes=30 - i * 5)).isoformat()
            records.append({'type': 'assistant', 'timestamp': timestamp,
                            'sessionId': 'synthetic-provider', 'requestId': f'r{i}',
                            'message': {'id': f'm{i}', 'model': 'synthetic-unpriced',
                                        'usage': {'input_tokens': 100, 'output_tokens': 20}}})
        with open(path, 'w') as output:
            output.write('\n'.join(json.dumps(r) for r in records) + '\n')
        ingest_file(conn=conn, path=path, source_adapter='claude_code', origin_harness='claude-code', account_alias='synthetic')
        for i, status in enumerate(('available', 'available', 'unavailable')):
            import_quota_snapshot(conn, {'provider': 'synthetic-official', 'official_source': f'headers-{i}',
                'measured_at': (now - timedelta(minutes=3-i)).isoformat(), 'window_type': 'hour',
                'limit_value': 100, 'used_value': i * 10, 'remaining_value': 100-i*10,
                'reset_at': (now + timedelta(hours=1)).isoformat(), 'status': status, 'units': 'requests'})
        import_quota_snapshot(conn, {'provider': 'expired', 'official_source': 'old-headers',
            'measured_at': (now - timedelta(days=2)).isoformat(), 'reset_at': (now-timedelta(days=1)).isoformat(),
            'status': 'available', 'percent_used': 20})
        conn.commit()
        conn.close()
        self.server = create_server(db_path=self.db, host='127.0.0.1', port=0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _, headers, bootstrap = self.call('GET', '/api/v1/bootstrap')
        self.auth = {'Origin': f'http://127.0.0.1:{self.port}', 'Cookie': headers['Set-Cookie'].split(';')[0], 'X-CSRF-Token': bootstrap['csrf_token']}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        client.request(method, path, json.dumps(body) if body is not None else None, {'Content-Type': 'application/json', **(headers or {})})
        response = client.getresponse()
        result = response.status, dict(response.getheaders()), json.loads(response.read())
        client.close()
        return result

    def query(self, metric):
        status, _, result = self.call('POST', '/api/v1/query', {'contract_version': '1', 'metric_id': metric}, self.auth)
        self.assertEqual(status, 200, result)
        return result

    def test_quota_history_burn_buckets_live_and_budget_persistence(self):
        cards = self.query('provider_quotas')['values']['quotas']
        quota = next(q for q in cards if q['provider'] == 'synthetic-official')
        self.assertFalse(quota['is_available'])
        self.assertEqual([h['provenance'] for h in quota['history']], ['headers-0', 'headers-1', 'headers-2'])
        self.assertEqual([h['used_value'] for h in quota['history']], [0, 10, 20])
        self.assertTrue(all(h['reported_percent_used'] is None for h in quota['history']))
        stale = next(q for q in cards if q['provider'] == 'expired')
        self.assertEqual(stale['status'], 'stale')
        self.assertIsNone(stale['percent_used'])
        self.assertEqual(stale['history'][0]['reported_percent_used'], 20)
        burn = self.query('token_burn_rate')['values']
        series = burn['recent_burn_series']
        self.assertGreaterEqual(len(series), 6)
        self.assertGreaterEqual(sum(b['request_count'] > 0 for b in series), 5)
        self.assertEqual([b['timestamp'] for b in series], sorted(b['timestamp'] for b in series))
        self.assertTrue(series[-1]['is_partial'])
        for bucket in series:
            self.assertEqual(bucket['denominator_minutes'], 5)
            self.assertIsNone(bucket['cost_usd'])
        self.assertIsNone(burn['current_burn_rate_cost_per_hour'])
        live = self.query('live_sessions')['values']['active_sessions'][0]
        self.assertIsNone(live['estimated_value_usd'])
        self.assertIsNotNone(live['last_sync_timestamp'])
        self.assertEqual(live['data_status'], 'partial_local_observations')
        for period, scope in [('project_model', {'model': 'synthetic-unpriced'}), ('session', {})]:
            status, _, saved = self.call('POST', '/api/v1/budgets', {'name': 'Synthetic guardrail', 'period': period, 'target_type': 'tokens', 'threshold': 1000, 'scope': scope}, self.auth)
            self.assertIn(status, (200, 201), saved)
        budgets = self.query('usage_budgets')['values']['budgets']
        model = next(b for b in budgets if b['scope'].get('model'))
        self.assertEqual(model['scope'], {'model': 'synthetic-unpriced'})
        self.assertGreater(model['current_value'], 0)
        session = next(b for b in budgets if b['period'] == 'session')
        self.assertEqual(session['comparison_status'], 'no_baseline')
        self.assertIsNone(session['comparison_baseline'])
        status, _, result = self.call('DELETE', '/api/v1/budgets/' + model['id'], headers=self.auth)
        self.assertEqual(status, 200, result)
        self.assertFalse(any(b['id'] == model['id'] for b in self.query('usage_budgets')['values']['budgets']))
