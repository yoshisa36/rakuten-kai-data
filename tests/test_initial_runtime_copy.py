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
        entries = [{'path': p, 'type': 'blob', 'mode': '100644', 'sha': self.add_blob(b)} for p, b in files.items()]
        tree = m.tree_identity({e['path']: e for e in entries})
        self.trees[tree] = entries
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
                return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': 'c' * 40}}
            return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': self.current}}
        if method == 'GET' and suffix.startswith('/git/commits/'):
            return copy.deepcopy(self.commits[(repo, suffix.split('/')[-1])])
        if method == 'GET' and suffix.startswith('/git/trees/'):
            sha = suffix.split('/')[-1].split('?')[0]
            return {'sha': sha, 'truncated': False, 'tree': copy.deepcopy(self.trees[sha])}
        if method == 'GET' and suffix.startswith('/git/blobs/'):
            raw = self.blobs[suffix.split('/')[-1]]
            if self.corrupt_verification and self.current != D0 and raw == self.source['products.json']:
                raw += b' '
            return {'sha': suffix.split('/')[-1], 'encoding': 'base64', 'content': base64.b64encode(raw).decode(), 'size': len(raw)}
        if method == 'POST' and suffix == '/git/blobs':
            return {'sha': self.add_blob(base64.b64decode(payload['content']))}
        if method == 'POST' and suffix == '/git/trees':
            entries = copy.deepcopy(self.trees[payload['base_tree']]) + copy.deepcopy(payload['tree'])
            sha = m.tree_identity({e['path']: e for e in entries})
            self.trees[sha] = entries
            return {'sha': sha, 'truncated': False, 'tree': [
                {'path': p, 'type': e[0], 'mode': e[1], 'sha': e[2]}
                for p, e in m.root_entries({e['path']: e for e in entries}).items()]}
        if method == 'POST' and suffix == '/git/commits':
            sha = self.new_sha()
            self.commits[(repo, sha)] = {'sha': sha, 'tree': {'sha': payload['tree']},
                                         'message': payload['message'], 'parents': [{'sha': p} for p in payload['parents']]}
            return copy.deepcopy(self.commits[(repo, sha)])
        if method == 'PATCH' and suffix == '/git/refs/heads/main':
            if payload['force'] is not False:
                raise AssertionError('Force update forbidden')
            self.current = payload['sha']
            if self.lose_ref_response:
                raise m.Stop('write_outcome_unknown')
            return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': self.current}}
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



class ResponseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.plan = m.prepare(self.api, S)
        self.approval = m.digest(m.canonical(self.plan))
        self.api.calls.clear()
        self.api.ref_reads = 0

    def mutated_apply(self, method, suffix, mutate):
        original = self.api.request
        def request(repo, actual_method, actual_suffix, payload=None):
            result = original(repo, actual_method, actual_suffix, payload)
            if repo == m.DESTINATION and (actual_method, actual_suffix) == (method, suffix):
                return mutate(result)
            return result
        with patch.object(self.api, 'request', side_effect=request), self.assertRaises(m.Stop):
            m.apply(self.api, self.plan, self.approval)
        if method != 'PATCH':
            self.assertEqual(self.api.current, D0)
            self.assertFalse(any(c[1] == 'PATCH' for c in self.api.calls))

    def test_metadata_nonobjects_and_missing_fields_stop(self):
        original = self.api.request
        for bad in (None, [], 'SYNTHETIC_SECRET', 3, {}, {'private': True}):
            with self.subTest(bad=bad):
                def request(repo, method, suffix, payload=None):
                    return bad if suffix == '/' else original(repo, method, suffix, payload)
                with patch.object(self.api, 'request', side_effect=request), self.assertRaises(m.Stop):
                    m.prepare(self.api, S)

    def test_ref_schema_sha_and_type_are_required(self):
        for bad in (None, {}, {'object': []}, {'ref': 'refs/heads/main', 'object': {'sha': 7, 'type': 'commit'}},
                    {'ref': 'refs/heads/main', 'object': {'sha': 'x' * 40, 'type': 'commit'}},
                    {'ref': 'refs/heads/other', 'object': {'sha': D0, 'type': 'commit'}},
                    {'ref': 'refs/heads/main', 'object': {'sha': D0, 'type': 'blob'}}):
            with self.subTest(bad=bad), self.assertRaises(m.Stop):
                m.validate_ref(bad)

    def test_commit_schema_requires_typed_tree_parents_message(self):
        valid = copy.deepcopy(self.api.commits[(m.SOURCE, S)])
        for key, value in (('sha', 'x' * 40), ('tree', None), ('tree', {'sha': 42}),
                           ('parents', {}), ('parents', [None]), ('message', [])):
            with self.subTest(key=key, value=value):
                bad = dict(valid, **{key: value})
                with self.assertRaises(m.Stop):
                    m.validate_commit(bad)
        for key in ('sha', 'tree', 'parents', 'message'):
            bad = copy.deepcopy(valid)
            del bad[key]
            with self.subTest(missing=key), self.assertRaises(m.Stop):
                m.validate_commit(bad)

    def test_get_commit_mismatched_identity_stops(self):
        original = self.api.request
        def request(repo, method, suffix, payload=None):
            result = original(repo, method, suffix, payload)
            if '/git/commits/' in suffix:
                result['sha'] = 'e' * 40
            return result
        with patch.object(self.api, 'request', side_effect=request), self.assertRaises(m.Stop):
            m.prepare(self.api, S)
        self.assertFalse(any(c[1] != 'GET' for c in self.api.calls))

    def test_tree_requires_sha_truncated_and_entry_types(self):
        entry = {'path': 'file.json', 'type': 'blob', 'mode': '100644', 'sha': 'd' * 40}
        valid = {'sha': 'e' * 40, 'truncated': False, 'tree': [entry]}
        for key, value in (('sha', 'wrong'), ('truncated', 0), ('tree', {}), ('tree', [None])):
            with self.subTest(key=key), self.assertRaises(m.Stop):
                m.validate_tree(dict(valid, **{key: value}))
        for key in ('sha', 'truncated', 'tree'):
            bad = dict(valid)
            del bad[key]
            with self.subTest(missing=key), self.assertRaises(m.Stop):
                m.validate_tree(bad)

    def test_tree_entries_bad_sha_path_mode_duplicates_stop(self):
        entry = {'path': 'file.json', 'type': 'blob', 'mode': '100644', 'sha': 'd' * 40}
        for key, value in (('sha', {}), ('path', []), ('path', '../bad'), ('mode', '120000'), ('type', 'commit')):
            with self.subTest(key=key), self.assertRaises(m.Stop):
                m.validate_tree({'sha': 'e' * 40, 'truncated': False, 'tree': [dict(entry, **{key: value})]})
        with self.assertRaises(m.Stop):
            m.validate_tree({'sha': 'e' * 40, 'truncated': False, 'tree': [entry, entry]})

    def test_get_blob_schema_and_identity_stop(self):
        original = self.api.request
        for field, value in (('sha', 'x' * 40), ('sha', 'e' * 40), ('size', True),
                             ('size', 999), ('content', []), ('content', '!!!'), ('encoding', None)):
            with self.subTest(field=field, value=value):
                def request(repo, method, suffix, payload=None):
                    result = original(repo, method, suffix, payload)
                    if '/git/blobs/' in suffix:
                        result[field] = value
                    return result
                with patch.object(self.api, 'request', side_effect=request), self.assertRaises(m.Stop):
                    m.prepare(self.api, S)
        self.assertEqual(self.api.current, D0)

    def test_created_blob_must_have_valid_sha(self):
        for bad in (None, [], {}, {'sha': None}, {'sha': 'x' * 40}):
            with self.subTest(bad=bad):
                self.mutated_apply('POST', '/git/blobs', lambda response: bad)
        self.assertFalse(any(c[2] == '/git/trees' and c[1] == 'POST' for c in self.api.calls))

    def test_created_blob_valid_but_wrong_sha_stops(self):
        self.mutated_apply('POST', '/git/blobs', lambda response: {'sha': 'e' * 40})
        self.assertEqual(sum(c[1] == 'POST' for c in self.api.calls), 1)

    def test_created_tree_malformed_response_stops(self):
        for bad in (None, {}, {'sha': 'x' * 40}, {'sha': 'd' * 40, 'truncated': False, 'tree': 'bad'}):
            with self.subTest(bad=bad):
                self.mutated_apply('POST', '/git/trees', lambda response: bad)
        self.assertFalse(any(c[2] == '/git/commits' and c[1] == 'POST' for c in self.api.calls))

    def test_created_tree_valid_wrong_identity_stops(self):
        self.mutated_apply('POST', '/git/trees', lambda response: dict(response, sha='e' * 40))

    def test_created_tree_response_contents_must_match(self):
        self.mutated_apply('POST', '/git/trees', lambda response: dict(response, tree=[]))
        self.assertFalse(any(c[1] == 'POST' and c[2] == '/git/commits' for c in self.api.calls))

    def test_created_tree_recursive_contents_are_verified(self):
        original = self.api.request
        created = False
        def request(repo, method, suffix, payload=None):
            nonlocal created
            result = original(repo, method, suffix, payload)
            if method == 'POST' and suffix == '/git/trees':
                created = True
            elif created and method == 'GET' and '/git/trees/' in suffix:
                result['tree'] = result['tree'][:-1]
            return result
        with patch.object(self.api, 'request', side_effect=request), self.assertRaises(m.Stop) as error:
            m.apply(self.api, self.plan, self.approval)
        self.assertEqual(error.exception.code, 'created_tree_contents_mismatch')
        self.assertEqual(self.api.current, D0)

    def test_created_commit_malformed_response_stops(self):
        for bad in (None, {}, {'sha': 'x' * 40}, {'sha': 'e' * 40}):
            with self.subTest(bad=bad):
                self.mutated_apply('POST', '/git/commits', lambda response: bad)

    def test_created_commit_wrong_tree_parent_or_message_stops(self):
        for key, value in (('tree', {'sha': 'e' * 40}), ('parents', [{'sha': 'e' * 40}]),
                           ('parents', []), ('message', 'unexpected')):
            with self.subTest(key=key):
                self.mutated_apply('POST', '/git/commits', lambda response: dict(response, **{key: value}))

    def test_created_commit_reloaded_before_patch(self):
        original = self.api.request
        def request(repo, method, suffix, payload=None):
            result = original(repo, method, suffix, payload)
            if repo == m.DESTINATION and method == 'GET' and '/git/commits/' in suffix and result['sha'] != D0:
                result['message'] = 'unexpected'
            return result
        with patch.object(self.api, 'request', side_effect=request), self.assertRaises(m.Stop):
            m.apply(self.api, self.plan, self.approval)
        self.assertEqual(self.api.current, D0)

    def test_patch_bad_response_stops_without_repeating(self):
        self.mutated_apply('PATCH', '/git/refs/heads/main', lambda response: [])
        self.assertEqual(sum(c[1] == 'PATCH' for c in self.api.calls), 1)

    def test_bad_api_response_cli_has_no_traceback_or_contents(self):
        for bad in (None, ['SYNTHETIC_SECRET'], {'full_name': 'SYNTHETIC_SECRET'}):
            api = FakeAPI()
            original = api.request
            def request(repo, method, suffix, payload=None):
                return bad if suffix == '/' else original(repo, method, suffix, payload)
            out, err = io.StringIO(), io.StringIO()
            with patch.object(api, 'request', side_effect=request), patch.object(m, 'GitHub', return_value=api), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(m.main(['--source-commit', S]), 2)
            self.assertEqual(err.getvalue(), '')
            self.assertNotIn('SYNTHETIC_SECRET', out.getvalue())
            self.assertEqual(json.loads(out.getvalue())['status'], 'stopped')

    def test_transport_nonobject_duplicate_json_and_lost_body_stop(self):
        api = m.GitHub('SYNTHETIC_SECRET')
        for raw in (b'null', b'[]', b'{"sha":1,"sha":2}', b'{"x":NaN}', b'{'):
            with self.subTest(raw=raw), patch.object(api.opener, 'open') as send:
                send.return_value.__enter__.return_value.read.return_value = raw
                with self.assertRaises(m.Stop):
                    api.request(m.DESTINATION, 'POST', '/git/blobs', {})
                self.assertEqual(send.call_count, 1)
        with patch.object(api.opener, 'open') as send:
            send.return_value.__enter__.return_value.read.side_effect = m.HTTPException('SYNTHETIC_SECRET')
            with self.assertRaises(m.Stop) as error:
                api.request(m.DESTINATION, 'PATCH', '/git/refs/heads/main', {})
            self.assertEqual(error.exception.code, 'write_outcome_unknown')
            self.assertEqual(send.call_count, 1)

    def test_known_git_tree_identity_and_nested_paths(self):
        self.assertEqual(m.tree_identity({}), '4b825dc642cb6eb9a060e54bf8d69288fbee4904')
        # Known Git blob identity for empty bytes, independent of the fake API.
        self.assertEqual(m.blob_identity(b''), 'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391')
        files = {'a/file.json': {'sha': m.blob_identity(b'{}')}, 'README.md': {'sha': m.blob_identity(b'doc')}}
        # Independently obtained with git hash-object -t tree --stdin, not this implementation.
        self.assertEqual(m.tree_identity(files), '343070cbbc3447046ddab76e1bdd2997d43294c1')
        self.assertEqual(m.tree_identity(files), m.tree_identity(dict(reversed(list(files.items())))))

if __name__ == '__main__':
    unittest.main()
