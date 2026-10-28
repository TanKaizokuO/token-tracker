"""Public selector parity and write visibility on a real synthetic SQLite store."""
import json
import unittest

from token_ledger.pricing import create_pricing_version, resolve_versioned_model_identity
from token_ledger.query import filtered_request_rows
from token_ledger.store import open_database


class SelectorIdentityTests(unittest.TestCase):
    def setUp(self):
        self.conn = open_database(':memory:')
        self.conn.execute("""INSERT INTO canonical_requests
            (canonical_id, provider, origin_harness, raw_model, normalized_model,
             request_timestamp, created_at)
            VALUES ('parent', 'openai', 'omp', 'unknown', 'unknown',
                    '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')""")
        self.query = {'contract_version': '1', 'metric_id': 'summary',
                      'harness_scope': 'combined', 'time_range': {
                          'start': '2025-01-01T00:00:00Z',
                          'end': '2028-01-01T00:00:00Z', 'timezone': 'UTC'}}

    def tearDown(self):
        self.conn.close()

    def version(self, ident, start, end=None, display='Sol 6.1'):
        create_pricing_version(self.conn, ident, [], start, effective_end=end,
                               source_provenance={'model_mappings': [{
                                   'provider': 'openai', 'access_route': 'direct',
                                   'alias': 'local-alias', 'model_identity': 'gpt-6.1',
                                   'display_name': display}]})
        self.conn.execute('UPDATE pricing_versions SET created_at=? WHERE version_id=?',
                          ('2026-01-01T00:00:00Z', ident))

    def request(self, ident, stamp, version=None, raw='local-alias', route='direct'):
        self.conn.execute('''INSERT INTO canonical_requests
            (canonical_id, provider, origin_harness, raw_model, normalized_model,
             request_timestamp, created_at, pricing_version_id, access_route,
             retry_attempt, parent_request_id)
            VALUES (?, 'openai', 'omp', ?, ?, ?, ?, ?, ?, 2, 'parent')''',
                          (ident, raw, raw, stamp, stamp, version, route))

    def test_dated_aliases_match_standalone_resolver_and_preserve_nulls(self):
        self.version('old', '2026-01-01T00:00:00Z', '2026-07-01T00:00:00Z', 'Old alias')
        self.version('new-a', '2026-07-01T00:00:00Z', display='Tie loser')
        self.version('new-z', '2026-07-01T00:00:00Z', display='New alias')
        create_pricing_version(self.conn, 'absent', [], '2020-01-01T00:00:00Z',
                               source_provenance={'model_mappings': []})
        cases = [
            ('before', '2025-12-31T23:59:59Z', None, 'local-alias', 'direct', None),
            ('start', '2026-01-01T00:00:00Z', None, 'local-alias', 'direct', 'Old alias'),
            ('end', '2026-07-01T00:00:00Z', None, 'local-alias', 'direct', 'New alias'),
            ('offset', '2026-06-30T20:00:00-04:00', None, 'local-alias', 'direct', 'New alias'),
            ('assigned', '2026-08-01T00:00:00Z', 'old', 'local-alias', 'direct', 'Old alias'),
            ('missing-version', '2026-08-01T00:00:00Z', 'absent', 'local-alias', 'direct', None),
            ('unknown', '2026-08-01T00:00:00Z', None, 'unmapped', 'direct', None),
            ('other-route', '2026-08-01T00:00:00Z', None, 'local-alias', 'default', None),
            ('default', '2026-08-01T00:00:00Z', None, 'gpt-6.1', 'direct', 'Sol 6.1'),
        ]
        for ident, stamp, version, raw, route, expected in cases:
            self.request(ident, stamp, version, raw, route)
        rows = {row['canonical_id']: row for row in filtered_request_rows(self.conn, self.query)}
        for ident, stamp, version, raw, route, expected in cases:
            with self.subTest(ident=ident):
                row = rows[ident]
                self.assertEqual(row['display_name'], expected)
                self.assertEqual(row['display_name'], resolve_versioned_model_identity(
                    self.conn, raw, 'openai', route, stamp, version).display_name)
                self.assertEqual(row['raw_model'], raw)
                self.assertIsNone(row['total_tokens'])
                self.assertIsNone(row['api_equivalent_usd'])
                self.assertEqual(row['pricing_status'], 'unpriced')
                self.assertEqual(row['retry_attempt'], 2)
                self.assertEqual(row['parent_request_id'], 'parent')

    def test_next_query_observes_pricing_mapping_and_request_writes(self):
        self.request('one', '2026-10-01T12:00:00Z')
        self.assertIsNone(filtered_request_rows(self.conn, self.query)[0]['display_name'])
        self.version('new', '2026-01-01T00:00:00Z')
        self.assertEqual(filtered_request_rows(self.conn, self.query)[0]['display_name'], 'Sol 6.1')
        provenance = {'model_mappings': [{'provider': 'openai', 'access_route': 'direct',
                      'alias': 'local-alias', 'model_identity': 'gpt-6.1', 'display_name': 'Revised'}]}
        self.conn.execute('UPDATE pricing_versions SET source_provenance_json=?',
                          (json.dumps(provenance),))
        self.assertEqual(filtered_request_rows(self.conn, self.query)[0]['display_name'], 'Revised')
        self.conn.execute("UPDATE canonical_requests SET raw_model='unknown'")
        self.assertIsNone(filtered_request_rows(self.conn, self.query)[0]['display_name'])
        self.conn.execute("UPDATE canonical_requests SET raw_model='local-alias'")
        self.conn.execute('DELETE FROM pricing_versions')
        self.assertIsNone(filtered_request_rows(self.conn, self.query)[0]['display_name'])

    def test_version_reads_do_not_scale_with_request_population(self):
        self.version('current', '2026-01-01T00:00:00Z')
        for i in range(100):
            self.request(str(i), f'2026-10-01T12:00:{i % 60:02d}Z')
        statements = []
        self.conn.set_trace_callback(statements.append)
        rows = filtered_request_rows(self.conn, self.query)
        self.conn.set_trace_callback(None)
        self.assertEqual(len(rows), 100)
        reads = [sql for sql in statements if 'FROM pricing_versions' in sql]
        self.assertEqual(len(reads), 1)


if __name__ == '__main__':
    unittest.main()
