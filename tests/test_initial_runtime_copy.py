"""Only synthetic bytes and in-memory APIs; never use repository production data."""
import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, 'path', [str(ROOT / 'scripts')] + sys.path):
    spec = importlib.util.spec_from_file_location('initial_copy', ROOT / 'scripts/initial_runtime_copy.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
S, D0 = 'a' * 40, 'b' * 40


class FakeAPI:
    def __init__(self):
        self.calls, self.blobs, self.trees, self.commits = [], {}, {}, {}
        self.counter = 100
        self.private = True
        self.failure = None
        self.lose_ref_response = False
        self.conflict_before_patch = False
        self.corrupt_verification = False
        queue = {'version': 1, 'updatedAt': '2026-01-01T00:00:00Z', 'posts': [
            {'reservationId': 'synthetic-id', 'text': 'SYNTHETIC_PRIVATE_TEXT',
             'status': 'posted', 'postId': 'synthetic-post', 'approvalStatus': 'approved',
             'unknown': {'keep': True}}]}
        self.source = {p: (json.dumps(queue if p in ('threads-queue.json', 'batches/2026-10-05.json')
                                      else {'synthetic': True}).encode() + b'\r\n') for p in m.PATHS}
        self.add_commit(m.SOURCE, S, self.source)
        self.add_commit(m.DESTINATION, D0, {'README.md': b'Synthetic README\r\n'})
        self.current = D0
        self.ref_reads = 0

    def new_sha(self):
        self.counter += 1
        return format(self.counter, '040x')

    def add_blob(self, raw):
        sha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        self.blobs[sha] = raw
        return sha

    def add_commit(self, repo, sha, files, message='', parents=None):
        tree = self.new_sha()
        self.trees[tree] = [{'path': p, 'type': 'blob', 'mode': '100644', 'sha': self.add_blob(b)} for p, b in files.items()]
        self.commits[(repo, sha)] = {'sha': sha, 'tree': {'sha': tree}, 'message': message,
                                     'parents': [{'sha': p} for p in (parents or [])]}

    def request(self, repo, method, suffix, payload=None):
        self.calls.append((repo, method, suffix, copy.deepcopy(payload)))
        if self.failure and (method, suffix) == self.failure[:2]:
            raise m.Stop(self.failure[2])
        if repo == m.SOURCE and method != 'GET':
            raise AssertionError('Source write attempted')
        if suffix == '/':
            return {'full_name': m.DESTINATION, 'private': self.private, 'default_branch': 'main'}
        if suffix == '/git/ref/heads/main':
            self.ref_reads += 1
            if self.conflict_before_patch and self.ref_reads >= 2:
                return {'object': {'sha': 'c' * 40}}
            return {'object': {'sha': self.current}}
        if method == 'GET' and suffix.startswith('/git/commits/'):
            return copy.deepcopy(self.commits[(repo, suffix.split('/')[-1])])
        if method == 'GET' and suffix.startswith('/git/trees/'):
            sha = suffix.split('/')[-1].split('?')[0]
            return {'truncated': False, 'tree': copy.deepcopy(self.trees[sha])}
        if method == 'GET' and suffix.startswith('/git/blobs/'):
            raw = self.blobs[suffix.split('/')[-1]]
            if self.corrupt_verification and self.current != D0 and raw == self.source['products.json']:
                raw += b' '
            return {'encoding': 'base64', 'content': base64.b64encode(raw).decode(), 'size': len(raw)}
        if method == 'POST' and suffix == '/git/blobs':
            return {'sha': self.add_blob(base64.b64decode(payload['content']))}
        if method == 'POST' and suffix == '/git/trees':
            sha = self.new_sha()
            self.trees[sha] = copy.deepcopy(self.trees[payload['base_tree']]) + copy.deepcopy(payload['tree'])
            return {'sha': sha}
        if method == 'POST' and suffix == '/git/commits':
            sha = self.new_sha()
            self.commits[(repo, sha)] = {'sha': sha, 'tree': {'sha': payload['tree']},
                                         'message': payload['message'], 'parents': [{'sha': p} for p in payload['parents']]}
            return {'sha': sha}
        if method == 'PATCH' and suffix == '/git/refs/heads/main':
            if payload['force'] is not False:
                raise AssertionError('Force update forbidden')
            self.current = payload['sha']
            if self.lose_ref_response:
                raise m.Stop('write_outcome_unknown')
            return {'object': {'sha': self.current}}
        raise AssertionError('Unexpected mock endpoint')


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.plan = m.prepare(self.api, S)
        self.approval = m.digest(m.canonical(self.plan))
        self.api.calls.clear()
        self.api.ref_reads = 0

    def writes(self):
        return [c for c in self.api.calls if c[1] != 'GET']

    def test_dry_run_has_no_writes_and_fixed_source(self):
        plan = m.prepare(self.api, S)
        self.assertEqual(plan, self.plan)
        self.assertFalse(self.writes())
        self.assertEqual([r['path'] for r in plan['files']], list(m.PATHS))
        self.assertTrue(all(c[1] == 'GET' for c in self.api.calls if c[0] == m.SOURCE))
        self.assertEqual({c[2] for c in self.api.calls if c[0] == m.SOURCE and '/git/commits/' in c[2]}, {'/git/commits/' + S})

    def test_single_commit_verified_and_binary_bytes_preserved(self):
        result = m.apply(self.api, self.plan, self.approval)
        self.assertEqual(result['status'], 'applied_and_verified')
        self.assertEqual(len(result['results']), 6)
        self.assertEqual(sum(c[2] == '/git/commits' for c in self.writes()), 1)
        self.assertEqual(sum(c[1] == 'PATCH' for c in self.writes()), 1)
        self.assertEqual(sum(c[2] == '/git/blobs' for c in self.writes()), 7)
        for raw in self.api.source.values():
            self.assertIn(raw, self.api.blobs.values())
        self.assertNotIn('SYNTHETIC_PRIVATE_TEXT', json.dumps(result))
        self.assertNotIn('synthetic-id', json.dumps(result))

    def test_approval_and_plan_mutations_fail_before_remote_calls(self):
        with self.assertRaises(m.Stop):
            m.apply(self.api, self.plan, '0' * 64)
        self.assertEqual(self.api.calls, [])
        for field, value in (('source_commit', 'c' * 40), ('destination_commit', 'd' * 40),
                             ('destination_repository', m.SOURCE), ('files', [])):
            with self.subTest(field=field):
                plan = copy.deepcopy(self.plan)
                plan[field] = value
                with self.assertRaises(m.Stop):
                    m.apply(self.api, plan, self.approval)
                self.assertFalse(self.writes())

    def test_source_hash_changes_stop_before_writes(self):
        plan = copy.deepcopy(self.plan)
        plan['files'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(m.Stop, ''):
            m.apply(self.api, plan, m.digest(m.canonical(plan)))
        self.assertFalse(self.writes())

    def test_public_destination_rejected(self):
        self.api.private = False
        with self.assertRaises(m.Stop):
            m.apply(self.api, self.plan, self.approval)
        self.assertFalse(self.writes())

    def test_unexpected_existing_target_or_other_file_rejected(self):
        for name in ('threads-queue.json', 'unexpected.txt'):
            with self.subTest(name=name):
                self.api.add_commit(m.DESTINATION, D0, {'README.md': b'Synthetic README\r\n', name: b'{}'})
                with self.assertRaises(m.Stop):
                    m.apply(self.api, self.plan, self.approval)
                self.assertFalse(self.writes())

    def test_readme_changed_rejected(self):
        self.api.add_commit(m.DESTINATION, D0, {'README.md': b'Changed'})
        with self.assertRaises(m.Stop):
            m.apply(self.api, self.plan, self.approval)
        self.assertFalse(self.writes())

    def test_conflict_before_publication_never_updates_ref(self):
        self.api.conflict_before_patch = True
        with self.assertRaises(m.Stop) as error:
            m.apply(self.api, self.plan, self.approval)
        self.assertEqual(error.exception.code, 'destination_conflict')
        self.assertFalse(any(c[1] == 'PATCH' for c in self.writes()))
        self.assertEqual(self.api.current, D0)

    def test_partial_object_creation_failure_keeps_ref(self):
        self.api.failure = ('POST', '/git/trees', 'http_500')
        with self.assertRaises(m.Stop):
            m.apply(self.api, self.plan, self.approval)
        self.assertEqual(self.api.current, D0)
        self.assertFalse(any(c[1] == 'PATCH' for c in self.writes()))

    def test_ref_response_loss_resolved_without_repeating_write(self):
        self.api.lose_ref_response = True
        self.assertEqual(m.apply(self.api, self.plan, self.approval)['status'], 'applied_and_verified')
        self.assertEqual(sum(c[1] == 'PATCH' for c in self.writes()), 1)

    def test_unresolved_ref_response_stops(self):
        self.api.failure = ('PATCH', '/git/refs/heads/main', 'write_outcome_unknown')
        with self.assertRaises(m.Stop) as error:
            m.apply(self.api, self.plan, self.approval)
        self.assertEqual(error.exception.code, 'ref_update_outcome_unresolved')
        self.assertEqual(self.api.current, D0)

    def test_rerun_is_verified_without_any_new_writes(self):
        first = m.apply(self.api, self.plan, self.approval)
        self.api.calls.clear()
        second = m.apply(self.api, self.plan, self.approval)
        self.assertEqual(second['status'], 'already_applied_and_verified')
        self.assertEqual(first['destination_commit'], second['destination_commit'])
        self.assertFalse(self.writes())

    def test_rerun_on_unrelated_commit_stops(self):
        self.api.add_commit(m.DESTINATION, 'c' * 40, {'README.md': b'unrelated'})
        self.api.current = 'c' * 40
        with self.assertRaises(m.Stop):
            m.apply(self.api, self.plan, self.approval)
        self.assertFalse(self.writes())

    def test_verification_corruption_never_reports_success(self):
        self.api.corrupt_verification = True
        with self.assertRaises(m.Stop):
            m.apply(self.api, self.plan, self.approval)

    def test_invalid_source_queue_is_rejected(self):
        data = {'posts': [{'reservationId': 'duplicate'}, {'reservationId': 'duplicate'}]}
        self.api.source['threads-queue.json'] = json.dumps(data).encode()
        self.api.add_commit(m.SOURCE, S, self.api.source)
        with self.assertRaises(m.Stop):
            m.prepare(self.api, S)
        self.assertFalse(self.writes())

    def test_write_status_errors_never_retried(self):
        for status in (401, 403, 409, 429, 500, 503):
            with self.subTest(status=status):
                self.api.calls.clear()
                self.api.failure = ('POST', '/git/blobs', 'http_' + str(status))
                with self.assertRaises(m.Stop):
                    m.apply(self.api, self.plan, self.approval)
                self.assertEqual(len(self.writes()), 1)
                self.assertEqual(self.api.current, D0)

    def test_cli_defaults_to_dry_run_and_never_writes(self):
        out = io.StringIO()
        with patch.object(m, 'GitHub', return_value=self.api), contextlib.redirect_stdout(out):
            self.assertEqual(m.main(['--source-commit', S]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report['status'], 'dry_run')
        self.assertEqual(report['plan_sha256'], self.approval)
        self.assertFalse(self.writes())

    def test_apply_without_explicit_approval_never_initializes_api(self):
        with patch.object(m, 'GitHub') as api, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['--apply-plan', 'synthetic.json']), 2)
            api.assert_not_called()

    def test_invalid_json_source_never_writes(self):
        for raw in (b'{', b'{"x":1,"x":2}', b'{"x":NaN}'):
            with self.subTest(raw=raw):
                self.api.source['products.json'] = raw
                self.api.add_commit(m.SOURCE, S, self.api.source)
                with self.assertRaises(m.Stop):
                    m.prepare(self.api, S)
                self.assertFalse(self.writes())

    def test_source_missing_required_path_never_writes(self):
        del self.api.source['products.json']
        self.api.add_commit(m.SOURCE, S, self.api.source)
        with self.assertRaises(m.Stop):
            m.prepare(self.api, S)
        self.assertFalse(self.writes())

    def test_missing_readme_never_writes(self):
        self.api.add_commit(m.DESTINATION, D0, {})
        with self.assertRaises(m.Stop):
            m.apply(self.api, self.plan, self.approval)
        self.assertFalse(self.writes())

    def test_snapshot_cli_mismatch_is_failure(self):
        with patch.object(m.verifier, 'compare', return_value={'status': 'mismatch'}) as compare:
            with self.assertRaises(m.Stop) as error:
                m.apply(self.api, self.plan, self.approval)
            self.assertEqual(error.exception.code, 'snapshot_verification_failed')
            compare.assert_called_once()

    def test_truncated_tree_never_writes(self):
        original = self.api.request
        def request(repo, method, suffix, payload=None):
            result = original(repo, method, suffix, payload)
            if '/git/trees/' in suffix:
                result['truncated'] = True
            return result
        with patch.object(self.api, 'request', side_effect=request), self.assertRaises(m.Stop):
            m.apply(self.api, self.plan, self.approval)
        self.assertFalse(self.writes())

    def test_cli_saved_dry_run_envelope_can_be_approved(self):
        envelope = {'status': 'dry_run', 'plan': self.plan, 'plan_sha256': self.approval,
                    'source_commit_verified_by_snapshot_cli': False}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'plan.json'
            path.write_bytes(m.canonical(envelope))
            out = io.StringIO()
            with patch.object(m, 'GitHub', return_value=self.api), contextlib.redirect_stdout(out):
                code = m.main(['--apply-plan', str(path), '--approve-plan', self.approval])
            self.assertEqual(code, 0)
            self.assertNotIn('SYNTHETIC_PRIVATE_TEXT', out.getvalue())


class TransportTests(unittest.TestCase):
    def api(self):
        return m.GitHub('SYNTHETIC_SECRET')

    def test_credentials_only_destination_and_no_source_writes(self):
        api = self.api()
        with patch.object(api.opener, 'open') as send:
            send.return_value.__enter__.return_value.read.return_value = b'{}'
            api.request(m.SOURCE, 'GET', '/')
            self.assertIsNone(send.call_args.args[0].get_header('Authorization'))
            api.request(m.DESTINATION, 'GET', '/')
            self.assertEqual(send.call_args.args[0].get_header('Authorization'), 'Bearer SYNTHETIC_SECRET')
            self.assertEqual(send.call_args.args[0].full_url, 'https://api.github.com/repos/' + m.DESTINATION)
            with self.assertRaises(m.Stop):
                api.request(m.SOURCE, 'POST', '/git/blobs', {})

    def test_get_retries_are_bounded_and_auth_denials_not_retried(self):
        for status, expected in ((401, 1), (403, 1), (409, 1), (429, 3), (500, 3), (503, 3)):
            with self.subTest(status=status):
                api = self.api()
                failure = HTTPError('https://example.invalid', status, 'SYNTHETIC_SECRET', {}, None)
                with patch.object(api.opener, 'open', side_effect=failure) as send, patch.object(m.time, 'sleep'):
                    with self.assertRaises(m.Stop) as error:
                        api.request(m.DESTINATION, 'GET', '/')
                    self.assertEqual(send.call_count, expected)
                    self.assertNotIn('SYNTHETIC_SECRET', error.exception.code)

    def test_mutating_transport_is_never_retried(self):
        api = self.api()
        with patch.object(api.opener, 'open', side_effect=URLError('SYNTHETIC_SECRET')) as send:
            with self.assertRaises(m.Stop) as error:
                api.request(m.DESTINATION, 'PATCH', '/git/refs/heads/main', {})
            self.assertEqual(error.exception.code, 'write_outcome_unknown')
            self.assertEqual(send.call_count, 1)

    def test_get_transport_failure_has_bounded_retries(self):
        api = self.api()
        with patch.object(api.opener, 'open', side_effect=URLError('SYNTHETIC_SECRET')) as send, patch.object(m.time, 'sleep'):
            with self.assertRaises(m.Stop) as error:
                api.request(m.DESTINATION, 'GET', '/')
            self.assertEqual(error.exception.code, 'transport_failure')
            self.assertEqual(send.call_count, 3)

    def test_redirects_do_not_forward_credentials(self):
        self.assertIsNone(m.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid'))

    def test_cli_no_token_fails_without_secret_output(self):
        out = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(out):
            self.assertEqual(m.main(['--source-commit', S]), 2)
        self.assertEqual(json.loads(out.getvalue())['error'], 'destination_token_required')

if __name__ == '__main__':
    unittest.main()
