"""Real CLI comparison consumers on disposable, synthetic application data."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from token_ledger.export import ReportDefinition, execute_report, get_report_definition, save_report_definition
from token_ledger.ingest.runner import ingest_file
from token_ledger.store.database import open_database


class TestReportComparison(unittest.TestCase):
    def test_cli_daily_weekly_monthly_partial_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'ledger.sqlite3'
            conn = open_database(str(db))
            scenarios = [
                ('daily', 'daily', '2026-03-09T04:00:00Z', {}, '2026-03-07T17:00:00Z', '2026-03-08T16:00:00Z', 86400, 82800),
                ('weekly', 'weekly', '2026-03-16T04:00:00Z', {}, '2026-03-03T17:00:00Z', '2026-03-10T16:00:00Z', 601200, 604800),
                ('monthly', 'monthly', '2026-04-01T04:00:00Z', {}, '2026-02-15T17:00:00Z', '2026-03-15T16:00:00Z', 2419200, 2674800),
                ('partial', 'daily', '2026-03-09T04:00:00Z', {'start': '2026-03-08T10:00:00', 'end': '2026-03-08T14:00:00'}, '2026-03-07T17:00:00Z', '2026-03-08T16:00:00Z', 14400, 14400),
            ]
            outputs = []
            for name, frequency, now, bounds, prior_at, current_at, prior_seconds, current_seconds in scenarios:
                source = root / (name + '.jsonl')
                events = []
                for label, at, amount in [('prior', prior_at, 100), ('current', current_at, 200)]:
                    events.append({'type': 'assistant', 'timestamp': at, 'requestId': name + label, 'sessionId': name,
                                   'message': {'id': name + label, 'model': 'synthetic-unpriced', 'content': [],
                                               'usage': {'input_tokens': amount, 'output_tokens': 10,
                                                         'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}}})
                source.write_text('\n'.join(json.dumps(event) for event in events) + '\n')
                ingest_file(conn, str(source), source_adapter='claude_code')
                report = save_report_definition(conn, ReportDefinition(item_id=name, name='Synthetic comparison sk-' + 'a' * 32,
                    frequency=frequency, filter_state={'metric_id': 'summary', 'filters': {'model': ['synthetic-unpriced'], 'session': [name]},
                    'time_range': {'timezone': 'America/New_York', **bounds}}))
                env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
                result = subprocess.run([sys.executable, '-m', 'token_ledger', 'reports', 'run-due', '--db', str(db),
                                         '--managed-root', str(root / name), '--now', now], env=env, capture_output=True, text=True, check=True)
                execution = next(item for item in json.loads(result.stdout)['executed'] if item['report_id'] == name)
                records = {item['export_format']: item for item in execution['exports']}
                self.assertEqual(set(records), {'csv', 'json', 'html', 'pdf', 'png'})
                payload = json.loads(Path(records['json']['file_path']).read_text())
                comparison = payload['comparison']
                self.assertEqual(comparison['period_a']['metrics']['requests_count'], 1)
                self.assertEqual(comparison['period_b']['metrics']['requests_count'], 1)
                self.assertEqual(comparison['period_a']['duration_seconds'], prior_seconds)
                self.assertEqual(comparison['period_b']['duration_seconds'], current_seconds)
                self.assertIsNone(comparison['period_b']['metrics']['api_equivalent_usd'])
                self.assertIsNone(comparison['deltas']['api_equivalent_usd']['delta'])
                self.assertEqual(comparison['deltas']['requests_count']['delta'], 0)
                self.assertTrue(comparison['assumptions'])
                for fmt, record in records.items():
                    raw = Path(record['file_path']).read_bytes()
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), record['sha256_hash'])
                    self.assertEqual(len(raw), record['byte_size'])
                    self.assertTrue(record['redaction_applied'])
                    self.assertNotIn(('sk-' + 'a' * 32).encode(), raw)
                    if fmt == 'pdf':
                        self.assertTrue(raw.startswith(b'%PDF-'))
                        self.assertIn(b'period_a', raw)
                    elif fmt == 'png':
                        self.assertTrue(raw.startswith(b'\x89PNG\r\n\x1a\n'))
                        self.assertIn(b'period_a', raw)
                    else:
                        self.assertIn(b'period_a', raw)
                outputs.append({'scenario': name, 'prior_seconds': prior_seconds, 'current_seconds': current_seconds,
                                'prior_requests': 1, 'current_requests': 1, 'formats': len(records),
                                'cost_delta': comparison['deltas']['api_equivalent_usd']['delta']})
                # Remove the successful definition so later historical CLI clocks do not rerun it.
                conn.execute('DELETE FROM alerts_and_reports WHERE item_id = ?', (report.item_id,))
                conn.commit()
            broken = save_report_definition(conn, ReportDefinition(item_id='failure', name='Failure', frequency='daily',
                filter_state={'metric_id': 'summary', 'filters': {}, 'time_range': {'start': '2026-03-09', 'end': '2026-03-08'}}))
            inventory_before = conn.execute('SELECT COUNT(*) FROM export_inventory').fetchone()[0]
            with self.assertRaises(ValueError):
                execute_report(conn, broken.item_id, managed_root=root / 'failure', now_iso='2026-03-10T00:00:00Z')
            persisted = get_report_definition(conn, broken.item_id)
            self.assertEqual(persisted.threshold_config['generation_state']['status'], 'failed')
            self.assertIsNone(persisted.last_generated_at)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM export_inventory').fetchone()[0], inventory_before)
            self.assertFalse((root / 'failure').exists())
            print('REPORT_COMPARISON_PROOF=' + json.dumps(outputs, sort_keys=True))

    def test_cli_comparison_unavailable_when_prior_lacks_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'ledger.sqlite3'
            conn = open_database(str(db))
            name = 'missing_prior'
            source = root / f'{name}.jsonl'
            events = [{'type': 'assistant', 'timestamp': '2026-03-08T16:00:00Z', 'requestId': name + 'current', 'sessionId': name,
                       'message': {'id': name + 'current', 'model': 'synthetic-unpriced', 'content': [],
                                   'usage': {'input_tokens': 200, 'output_tokens': 10,
                                             'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}}}]
            source.write_text('\n'.join(json.dumps(event) for event in events) + '\n')
            ingest_file(conn, str(source), source_adapter='claude_code')
            report = save_report_definition(conn, ReportDefinition(item_id=name, name='Synthetic missing prior sk-' + 'a' * 32,
                frequency='daily', filter_state={'metric_id': 'summary', 'filters': {'model': ['synthetic-unpriced'], 'session': [name]},
                'time_range': {'timezone': 'America/New_York'}}))
            env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
            result = subprocess.run([sys.executable, '-m', 'token_ledger', 'reports', 'run-due', '--db', str(db),
                                     '--managed-root', str(root / name), '--now', '2026-03-09T04:00:00Z'], env=env, capture_output=True, text=True, check=True)
            execution = next(item for item in json.loads(result.stdout)['executed'] if item['report_id'] == name)
            records = {item['export_format']: item for item in execution['exports']}
            self.assertEqual(set(records), {'csv', 'json', 'html', 'pdf', 'png'})
            payload = json.loads(Path(records['json']['file_path']).read_text())
            self.assertEqual(payload['comparison'], 'unavailable')
            csv_raw = Path(records['csv']['file_path']).read_text()
            self.assertIn('comparison,unavailable', csv_raw)
            html_raw = Path(records['html']['file_path']).read_text()
            self.assertIn('<pre>unavailable</pre>', html_raw)
            for fmt in ('pdf', 'png'):
                raw = Path(records[fmt]['file_path']).read_bytes()
                self.assertIn(b'unavailable', raw)
            conn.close()
