import copy
import importlib.util
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('queue', ROOT / 'scripts/register_threads_queue.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime(2026, 10, 4, 12, 38, tzinfo=timezone.utc)

class FakeAPI:
    def __init__(self, queue, failure=None):
        self.queue = copy.deepcopy(queue); self.failure = failure; self.writes = 0
    def read(self):
        return copy.deepcopy(self.queue), 'sha'
    def write(self, queue, sha):
        self.writes += 1
        if self.failure:
            code = self.failure; self.failure = None
            raise HTTPError('https://api.github.com', code, 'test', {}, None)
        self.queue = copy.deepcopy(queue)
        return {'commit': {'sha': 'commit'}}

class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.batch = json.loads((ROOT / 'batches/2026-10-05.json').read_text())
        self.queue = {'version': 1, 'updatedAt': 'old', 'posts': []}
    def test_register_verify_and_idempotent(self):
        api = FakeAPI(self.queue)
        self.assertEqual(m.register(api, self.batch, NOW)['status'], 'registered_and_verified')
        self.assertEqual(m.register(api, self.batch, NOW)['status'], 'already_registered')
        self.assertEqual(api.writes, 1)
    def test_preserves_existing_status_and_unknown_fields(self):
        merged, _ = m.merge(self.queue, self.batch, NOW)
        merged['posts'][0]['status'] = '投稿済み'
        merged['posts'][0]['postId'] = '123'
        out, added = m.merge(merged, self.batch, NOW)
        self.assertFalse(added)
        self.assertEqual(out, merged)
    def test_changed_id_and_duplicate_slot_fail(self):
        merged, _ = m.merge(self.queue, self.batch, NOW)
        merged['posts'][0]['text'] = 'different'
        with self.assertRaises(ValueError): m.merge(merged, self.batch, NOW)
        merged['posts'][0]['reservationId'] = 'OTHER'
        with self.assertRaises(ValueError): m.merge(merged, self.batch, NOW)
    def test_rejects_unapproved_test_and_past(self):
        self.batch['posts'][0]['approvalStatus'] = 'pending'
        with self.assertRaises(ValueError): m.merge(self.queue, self.batch, NOW)
        self.batch['posts'][0]['approvalStatus'] = 'approved'
        self.batch['posts'][0]['postType'] = 'テスト'
        with self.assertRaises(ValueError): m.merge(self.queue, self.batch, NOW)
        self.batch['posts'][0]['postType'] = '商品'
        with self.assertRaises(ValueError): m.merge(self.queue, self.batch, datetime(2026, 10, 6, tzinfo=timezone.utc))
    def test_conflict_retries_permission_denial_stops(self):
        api = FakeAPI(self.queue, 409)
        m.register(api, self.batch, NOW)
        self.assertEqual(api.writes, 2)
        api = FakeAPI(self.queue, 403)
        with self.assertRaises(HTTPError): m.register(api, self.batch, NOW)
        self.assertEqual(api.writes, 1)
    def test_missing_write_verification_fails(self):
        api = FakeAPI(self.queue)
        api.write = lambda q, s: {'commit': {'sha': 'commit'}}
        with self.assertRaises(RuntimeError): m.register(api, self.batch, NOW)

if __name__ == '__main__': unittest.main()
