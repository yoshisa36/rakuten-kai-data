"""Exercise the monitor with fixed time and in-memory input, never live data."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('freshness', ROOT / 'scripts/check_rakuten_freshness.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
NOW = dt.datetime(2026, 10, 19, 0, 0, tzinfo=dt.timezone.utc)

class FrozenDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def fixture():
    return {
        'generatedAt': '2026-10-18T23:00:00Z',
        'monitor': {
            'monthly': {'driveUpdatedAt': '2026-10-18T21:00:00Z'},
            'shop': {'driveUpdatedAt': '2026-10-18T21:00:00Z'},
            'gasSyncLastSuccess': '2026-10-18T22:00:00Z',
        },
        'daily': [{'date': '2026-10-18', 'clicks': 1}],
    }

class FreshnessTests(unittest.TestCase):
    def run_monitor(self, data):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(monitor.dt, 'datetime', FrozenDatetime), \
             patch.object(monitor.Path, 'read_text', return_value=json.dumps(data)), \
             patch.object(monitor.urllib.request, 'urlopen', side_effect=AssertionError('Network forbidden')) as network, \
             patch('sys.argv', ['check_rakuten_freshness.py', '--file', 'fixed.json']), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = monitor.main()
        network.assert_not_called()
        return result, stdout.getvalue() + stderr.getvalue()

    def test_healthy(self):
        code, output = self.run_monitor(fixture())
        self.assertEqual(code, 0)
        self.assertIn('楽天データ連携正常', output)

    def test_expired_csv_with_recent_export_and_sync(self):
        for name in ('monthly', 'shop'):
            with self.subTest(csv=name):
                data = fixture()
                data['monitor'][name]['driveUpdatedAt'] = '2026-10-12T00:00:00Z'
                code, output = self.run_monitor(data)
                self.assertEqual(code, 1)
                self.assertIn('36時間以上更新なし', output)

    def test_future_timestamp(self):
        data = fixture()
        data['generatedAt'] = '2026-10-19T00:16:00Z'
        code, output = self.run_monitor(data)
        self.assertEqual(code, 1)
        self.assertIn('未来の日時', output)

    def test_missing_timestamp_values(self):
        for path in (('generatedAt',), ('monitor', 'monthly', 'driveUpdatedAt'),
                     ('monitor', 'shop', 'driveUpdatedAt'), ('monitor', 'gasSyncLastSuccess')):
            for value in (None, ''):
                with self.subTest(path=path, value=value):
                    data = fixture()
                    parent = data
                    for key in path[:-1]:
                        parent = parent[key]
                    parent[path[-1]] = value
                    code, output = self.run_monitor(data)
                    self.assertEqual(code, 2)
                    self.assertIn('日時がありません', output)
            with self.subTest(path=path, absent=True):
                data = fixture()
                parent = data
                for key in path[:-1]:
                    parent = parent[key]
                del parent[path[-1]]
                self.assertEqual(self.run_monitor(data)[0], 2)

    def test_timezone_required(self):
        data = fixture()
        data['generatedAt'] = '2026-10-18T23:00:00'
        code, output = self.run_monitor(data)
        self.assertEqual(code, 2)
        self.assertIn('タイムゾーンがありません', output)

    def test_age_boundaries(self):
        for hours, expected_errors in ((36, 0), (36.01, 1), (-0.25, 0), (-0.26, 1)):
            with self.subTest(hours=hours), contextlib.redirect_stdout(io.StringIO()):
                errors = []
                monitor.check_age('固定日時', (NOW - dt.timedelta(hours=hours)).isoformat(), NOW, 36, errors)
                self.assertEqual(len(errors), expected_errors)

    def test_pipeline_order(self):
        for change, expected in ((('gasSyncLastSuccess', '2026-10-18T20:00:00Z'), 'CSV更新後'),
                                 (('generatedAt', '2026-10-18T21:30:00Z'), 'GAS取り込み後')):
            with self.subTest(field=change[0]):
                data = fixture()
                target = data if change[0] == 'generatedAt' else data['monitor']
                target[change[0]] = change[1]
                code, output = self.run_monitor(data)
                self.assertEqual(code, 1)
                self.assertIn(expected, output)

    def test_missing_monitor_structure(self):
        for key in ('monitor', 'monthly', 'shop'):
            with self.subTest(key=key):
                data = fixture()
                del (data if key == 'monitor' else data['monitor'])[key]
                self.assertEqual(self.run_monitor(data)[0], 2)

    def test_missing_daily(self):
        data = fixture()
        data['daily'] = []
        code, output = self.run_monitor(data)
        self.assertEqual(code, 1)
        self.assertIn('日別実績がありません', output)

if __name__ == '__main__':
    unittest.main()
