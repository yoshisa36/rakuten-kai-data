"""Read artificial JSON fixtures only; no freshness or remote state assertions."""
import datetime as dt
import importlib.util
import json
import math
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures'
spec = importlib.util.spec_from_file_location('registration_rules', ROOT / 'scripts/register_threads_queue.py')
registration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registration)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON object key: ' + key)
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('Non-standard JSON number: ' + value)


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8-sig'),
                      object_pairs_hook=reject_duplicate_keys, parse_constant=reject_constant)

class JsonIntegrityTests(unittest.TestCase):
    def timestamp(self, value):
        self.assertIsInstance(value, str)
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        self.assertIsNotNone(parsed.tzinfo)
        return parsed

    def nonnegative(self, value):
        self.assertNotIsInstance(value, bool)
        self.assertIsInstance(value, (int, float))
        self.assertTrue(math.isfinite(value))
        self.assertGreaterEqual(value, 0)

    def test_all_json_is_strict(self):
        paths = sorted(FIXTURES.glob('*.json')) + sorted((FIXTURES / 'batches').glob('*.json'))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(file=path.name):
                self.assertIsInstance(load(path.relative_to(FIXTURES)), dict)

    def test_counted_exports(self):
        for name, key in (('products.json', 'products'), ('selection-history.json', 'products'),
                          ('threads-analysis.json', 'posts')):
            with self.subTest(file=name):
                data = load(name)
                self.assertEqual(data['status'], 'ok')
                self.assertIsInstance(data[key], list)
                self.assertEqual(data['count'], len(data[key]))
                self.timestamp(data['updatedAt'])

    def test_queue_ids_slots_and_approved_content(self):
        data = load('threads-queue.json')
        self.assertEqual(data['version'], 1)
        self.timestamp(data['updatedAt'])
        self.assertIsInstance(data['posts'], list)
        ids, slots = set(), set()
        for post in data['posts']:
            with self.subTest(reservation=post['reservationId']):
                self.assertNotIn(post['reservationId'], ids)
                ids.add(post['reservationId'])
                scheduled = self.timestamp(post['scheduledAt'])
                self.assertIsInstance(post['status'], str)
                self.assertTrue(post['status'])
                # Legacy test entries are deliberately not auto-approved or rewritten.
                if 'テスト' in post['postType']:
                    self.assertNotEqual(post.get('approvalStatus'), 'approved')
                    continue
                self.assertNotIn(scheduled, slots)
                slots.add(scheduled)
                self.assertIn(post.get('approvalStatus'), ('approved', 'pending', None))
                if post.get('approvalStatus') == 'approved':
                    candidate = dict(post, status='承認待ち')
                    registration.validate_batch({'posts': [candidate]})

    def test_approved_batches_follow_registration_rules(self):
        paths = sorted((FIXTURES / 'batches').glob('*.json'))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(batch=path.name):
                # validate_batch has no wall-clock or API calls; old examples stay valid.
                registration.validate_batch(load(path.relative_to(FIXTURES)))

    def test_threads_metrics_and_identity(self):
        posts = load('threads-analysis.json')['posts']
        ids = set()
        for post in posts:
            with self.subTest(post=post['postId']):
                self.assertIsInstance(post['postId'], str)
                self.assertTrue(post['postId'])
                self.assertNotIn(post['postId'], ids)
                ids.add(post['postId'])
                self.timestamp(post['timestamp'])
                for key in ('views', 'likes', 'replies', 'reposts'):
                    self.nonnegative(post[key])
                self.assertIsInstance(post['captureCount'], int)
                self.assertGreaterEqual(post['captureCount'], 1)

    def test_rakuten_structure_without_freshness_requirement(self):
        data = load('rakuten-affiliate.json')
        self.assertEqual(data['schemaVersion'], 1)
        self.timestamp(data['generatedAt'])
        self.timestamp(data['monitor']['gasSyncLastSuccess'])
        for key in ('monthly', 'shop'):
            self.timestamp(data['monitor'][key]['driveUpdatedAt'])
        for key in ('clicks', 'sales', 'amount', 'rewards'):
            self.nonnegative(data['summary'][key])
        dates = set()
        for row in data['daily']:
            date = dt.date.fromisoformat(row['date'])
            self.assertNotIn(date, dates)
            dates.add(date)
        for key in ('daily', 'shops'):
            self.assertIsInstance(data[key], list)
            for row in data[key]:
                for metric in ('clicks', 'sales', 'amount', 'rewards'):
                    self.nonnegative(row[metric])

if __name__ == '__main__':
    unittest.main()
