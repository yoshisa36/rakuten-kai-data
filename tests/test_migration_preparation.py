"""Offline migration contracts; no production registration target changes."""
import contextlib
import copy
import io
import json
import socket
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from test_rakuten_freshness import monitor, fixture, FrozenDatetime
from test_register_threads_queue import m, FakeAPI, NOW
from test_json_integrity import load
from run_offline import deny_network
from migration_helpers import same_snapshot


class MonitorSourceTests(unittest.TestCase):
    def invoke(self, args):
        with patch.object(monitor.dt, 'datetime', FrozenDatetime), \
             patch('sys.argv', ['monitor'] + args), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return monitor.main()

    def test_default_url_is_unchanged(self):
        with patch.object(monitor.urllib.request, 'urlopen') as get:
            get.return_value.__enter__.return_value.read.return_value = json.dumps(fixture()).encode()
            self.assertEqual(self.invoke([]), 0)
            req = get.call_args.args[0]
            self.assertEqual(req.full_url, 'https://raw.githubusercontent.com/yoshisa36/rakuten-kai-data/main/rakuten-affiliate.json')
            self.assertEqual(get.call_args.kwargs['timeout'], 15)

    def test_explicit_url_uses_mock_only(self):
        with patch.object(monitor.urllib.request, 'urlopen') as get:
            get.return_value.__enter__.return_value.read.return_value = json.dumps(fixture()).encode()
            self.assertEqual(self.invoke(['--url', 'https://example.invalid/data.json']), 0)
            self.assertEqual(get.call_args.args[0].full_url, 'https://example.invalid/data.json')

    def test_file_precedes_url(self):
        with patch.object(monitor.Path, 'read_text', return_value=json.dumps(fixture())) as read, \
             patch.object(monitor.urllib.request, 'urlopen') as get:
            self.assertEqual(self.invoke(['--file', 'synthetic.json', '--url', 'https://example.invalid/data.json']), 0)
            read.assert_called_once_with(encoding='utf-8-sig')
            get.assert_not_called()

    def test_invalid_url_fails_before_network(self):
        for url in ('http://example.invalid/data', 'https://user:placeholder@example.invalid/data', 'file:///fixture'):
            with self.subTest(url=url), patch.object(monitor.urllib.request, 'urlopen') as get:
                self.assertEqual(self.invoke(['--url', url]), 2)
                get.assert_not_called()


class MigrationContractTests(unittest.TestCase):
    def test_json_comparison_preserves_content_and_timestamps(self):
        original = load('threads-queue.json')
        copied = json.loads(json.dumps(original, ensure_ascii=False, sort_keys=True))
        self.assertTrue(same_snapshot(json.dumps(original), json.dumps(copied)))
        self.assertEqual(original['updatedAt'], copied['updatedAt'])
        self.assertEqual([p['reservationId'] for p in original['posts']],
                         [p['reservationId'] for p in copied['posts']])
        for key, value in (('text', 'changed'), ('approvalStatus', 'pending'),
                           ('scheduledAt', '2026-10-06T11:30:00+09:00'), ('reservationId', 'changed')):
            with self.subTest(key=key):
                changed = copy.deepcopy(copied)
                changed['posts'][0][key] = value
                self.assertFalse(same_snapshot(json.dumps(original), json.dumps(changed)))

    def test_comparison_rejects_duplicate_keys_and_nonstandard_numbers(self):
        for invalid in ('{"id": 1, "id": 2}', '{"views": NaN}'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                same_snapshot(invalid, '{}')
        self.assertFalse(same_snapshot('{"approved": true}', '{"approved": 1}'))
        self.assertFalse(same_snapshot('[1, 2]', '[2, 1]'))

    def test_duplicate_batch_ids_are_rejected(self):
        batch = load('batches/approved.json')
        batch['posts'].append(copy.deepcopy(batch['posts'][0]))
        with self.assertRaises(ValueError):
            m.validate_batch(batch)

    def test_ids_and_published_fields_survive_conflict_retry(self):
        batch = load('batches/approved.json')
        published = copy.deepcopy(batch['posts'][0])
        published.update(status='投稿済み', postId='fixture-published', extra={'preserve': True})
        api = FakeAPI({'version': 1, 'updatedAt': 'old', 'posts': [published]}, 409)
        result = m.register(api, batch, NOW)
        self.assertEqual(result['status'], 'registered_and_verified')
        self.assertEqual(api.writes, 2)
        self.assertEqual(api.queue['posts'][0], published)
        self.assertEqual([p['reservationId'] for p in api.queue['posts']],
                         [p['reservationId'] for p in batch['posts']])

    def test_authentication_denial_stops_without_retry(self):
        for status in (401, 403):
            with self.subTest(status=status):
                api = FakeAPI({'version': 1, 'updatedAt': 'old', 'posts': []}, status)
                with self.assertRaises(HTTPError) as error:
                    m.register(api, load('batches/approved.json'), NOW)
                self.assertEqual(error.exception.code, status)
                self.assertEqual(api.writes, 1)
                self.assertEqual(api.queue['posts'], [])

    def test_current_registration_destination_contract(self):
        # Alternate repository selection remains a design only; do not patch REPO.
        api = m.GitHub('synthetic-placeholder', 'main')
        with patch.object(api, 'request', return_value={}) as request:
            api.write({'version': 1, 'posts': []}, 'fixture-sha')
        self.assertEqual(m.REPO, 'yoshisa36/rakuten-kai-data')
        method, path, payload = request.call_args.args
        self.assertEqual((method, path), ('PUT', '/contents/threads-queue.json'))
        self.assertEqual(payload['branch'], 'main')
        self.assertEqual(payload['sha'], 'fixture-sha')

    def test_default_transport_is_mocked_and_unchanged(self):
        api = m.GitHub('synthetic-placeholder', 'main')
        with patch.object(m, 'urlopen') as send:
            send.return_value.__enter__.return_value = io.StringIO('{}')
            api.request('GET', '/contents/threads-queue.json?ref=main')
        self.assertEqual(send.call_args.args[0].full_url,
                         'https://api.github.com/repos/yoshisa36/rakuten-kai-data/contents/threads-queue.json?ref=main')

    def test_runner_blocks_network(self):
        # Explicitly install the same guard so direct unittest discovery stays offline too.
        with patch.object(socket, 'create_connection', deny_network):
            with self.assertRaisesRegex(AssertionError, 'Network access is forbidden'):
                socket.create_connection(('example.invalid', 443))

if __name__ == '__main__':
    unittest.main()
