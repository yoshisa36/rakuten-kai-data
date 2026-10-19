"""Artificial local files only; run under tests/run_offline.py networking guards."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('snapshot', ROOT / 'scripts/verify_snapshot.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
COMMIT = 'a' * 40


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.destination = self.root / 'source', self.root / 'destination'
        self.source.mkdir()
        self.destination.mkdir()
        self.path = 'threads-queue.json'
        self.queue = {'version': 1, 'updatedAt': '2026-10-01T00:00:00Z', 'posts': [
            {'reservationId': 'artificial-reservation', 'text': 'ARTIFICIAL_PRIVATE_CONTENT',
             'scheduledAt': '2026-10-02T00:00:00Z', 'approvalStatus': 'approved',
             'status': 'posted', 'postId': 'artificial-post', 'unknown': {'nested': [1, True]}}]}
        self.write_both(self.queue)

    def write_both(self, value):
        raw = json.dumps(value).encode()
        for directory in (self.source, self.destination):
            (directory / self.path).write_bytes(raw)
        return raw

    def invoke(self, extra=None):
        argv = ['--source-root', str(self.source), '--destination-root', str(self.destination),
                '--path', self.path, '--source-commit', COMMIT]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = m.main(argv + (extra or []))
        self.assertEqual(err.getvalue(), '')
        self.assertNotIn('ARTIFICIAL_PRIVATE_CONTENT', out.getvalue())
        self.assertNotIn('artificial-reservation', out.getvalue())
        self.assertNotIn('artificial-post', out.getvalue())
        return code, json.loads(out.getvalue())

    def test_identical_bytes_and_structure(self):
        raw = self.write_both(self.queue)
        code, report = self.invoke()
        self.assertEqual(code, 0)
        result = report['results'][0]
        self.assertTrue(result['sha256_equal'])
        self.assertTrue(result['size_equal'])
        self.assertTrue(result['json_structure_equal'])
        self.assertEqual(result['source']['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result['source']['size'], len(raw))

    def test_format_only_difference_is_failure(self):
        (self.destination / self.path).write_text(json.dumps(self.queue, indent=2, sort_keys=True))
        code, report = self.invoke()
        self.assertEqual(code, 1)
        result = report['results'][0]
        self.assertFalse(result['sha256_equal'])
        self.assertTrue(result['json_structure_equal'])

    def test_same_size_modified_bytes(self):
        self.path = 'simple.json'
        self.write_both({'value': 'a'})
        (self.destination / self.path).write_text('{"value": "b"}')
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertTrue(report['results'][0]['size_equal'])
        self.assertFalse(report['results'][0]['sha256_equal'])

    def test_all_reservation_fields_and_unknown_values_detected(self):
        for field in ('reservationId', 'text', 'scheduledAt', 'approvalStatus', 'status', 'postId', 'unknown'):
            with self.subTest(field=field):
                value = copy.deepcopy(self.queue)
                value['posts'][0][field] = {'changed': True} if field == 'unknown' else 'ARTIFICIAL_CHANGE'
                (self.destination / self.path).write_text(json.dumps(value))
                code, report = self.invoke()
                self.assertEqual(code, 1)
                self.assertFalse(report['results'][0]['json_structure_equal'])

    def test_missing_and_extra_reservations(self):
        (self.destination / self.path).write_text(json.dumps(dict(self.queue, posts=[])))
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(report['results'][0]['queue']['missing_in_destination'], 1)
        self.assertEqual(report['results'][0]['queue']['extra_in_destination'], 0)
        value = copy.deepcopy(self.queue)
        value['posts'][0]['reservationId'] = 'other-artificial-id'
        (self.destination / self.path).write_text(json.dumps(value))
        self.assertEqual(self.invoke()[1]['results'][0]['queue']['extra_in_destination'], 1)

    def test_duplicate_reservation_ids_fail_even_when_identical(self):
        value = copy.deepcopy(self.queue)
        value['posts'].append(copy.deepcopy(value['posts'][0]))
        self.write_both(value)
        code, report = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(report['results'][0]['queue']['source']['error'], 'duplicate_reservation_id')

    def test_missing_id_or_invalid_queue_fail_even_when_identical(self):
        for value in ({'posts': [{}]}, {'posts': 'wrong'}, {'posts': [{'reservationId': ''}]}, {'posts': [None]}):
            with self.subTest(value=value):
                self.write_both(value)
                self.assertEqual(self.invoke()[0], 2)

    def test_invalid_json_and_duplicate_keys_and_constants(self):
        for raw in (b'{', b'{"secret-key":1,"secret-key":2}', b'{"n":NaN}',
                    b'{"n":Infinity}', b'{"n":-Infinity}', b'\xff'):
            with self.subTest(raw=raw):
                (self.destination / self.path).write_bytes(raw)
                code, report = self.invoke()
                self.assertEqual(code, 2)
                self.assertIsNone(report['results'][0]['json_structure_equal'])
                self.assertNotIn('secret-key', json.dumps(report))

    def test_missing_files(self):
        (self.destination / self.path).unlink()
        code, report = self.invoke()
        self.assertEqual(code, 2)
        self.assertFalse(report['results'][0]['destination']['read_ok'])

    def test_symbolic_file_rejected(self):
        (self.destination / self.path).unlink()
        (self.destination / self.path).symlink_to(self.source / self.path)
        self.assertEqual(self.invoke()[0], 2)

    def test_symbolic_directory_and_root_rejected(self):
        (self.destination / 'linked').symlink_to(self.source, target_is_directory=True)
        self.path = 'linked/threads-queue.json'
        self.assertEqual(self.invoke()[0], 2)
        self.path = 'threads-queue.json'
        self.destination = self.destination / 'linked'
        self.assertEqual(self.invoke()[0], 2)

    def test_traversal_absolute_and_unsafe_paths(self):
        for path in ('../threads-queue.json', '/tmp/data.json', 'a/../data.json', 'a//data.json',
                     './data.json', 'https://example.invalid/data.json', 'line\nbreak.json', 'data.txt'):
            with self.subTest(path=path):
                self.path = path
                self.assertEqual(self.invoke()[0], 2)

    def test_directory_and_fifo_rejected(self):
        (self.destination / self.path).unlink()
        (self.destination / self.path).mkdir()
        self.assertEqual(self.invoke()[0], 2)
        (self.destination / self.path).rmdir()
        os.mkfifo(self.destination / self.path)
        self.assertEqual(self.invoke()[0], 2)

    def test_size_limit(self):
        with patch.object(m, 'MAX_BYTES', 4):
            self.assertEqual(self.invoke()[0], 2)

    def test_commit_metadata_not_provenance(self):
        code, report = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(report['source_commit'], COMMIT)
        self.assertFalse(report['source_commit_verified'])
        for bad in ('not-a-sha', 'x' * 40, 'a' * 39):
            with self.subTest(commit=bad):
                self.assertEqual(self.invoke(['--source-commit', bad])[0], 2)

    def test_unknown_cli_argument_does_not_echo_value(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(m.main(['--secret=ARTIFICIAL_SECRET']), 2)
        self.assertNotIn('ARTIFICIAL_SECRET', out.getvalue() + err.getvalue())

    def test_deeply_nested_input_fails_without_traceback(self):
        (self.destination / self.path).write_bytes(b'[' * 2000 + b'0' + b']' * 2000)
        self.assertEqual(self.invoke()[0], 2)

    def test_nested_type_and_array_changes(self):
        self.path = 'generic.json'
        for left, right in (({'x': True}, {'x': 1}), ({'x': [1, 2]}, {'x': [2, 1]}),
                            ({'x': {'y': 1}}, {'x': {}}), ({'x': None}, {})):
            with self.subTest(left=left):
                self.write_both(left)
                (self.destination / self.path).write_text(json.dumps(right))
                self.assertEqual(self.invoke()[0], 1)

    def test_decimal_precision_is_preserved(self):
        self.path = 'generic.json'
        (self.source / self.path).write_bytes(b'{"x": 1.00000000000000001}')
        (self.destination / self.path).write_bytes(b'{"x": 1.00000000000000002}')
        self.assertFalse(self.invoke()[1]['results'][0]['json_structure_equal'])

    def assert_numeric_comparison(self, left, right, structure_equal, code):
        self.path = 'numeric.json'
        source = ('{"x": ' + left + '}').encode('ascii')
        destination = ('{"x": ' + right + '}').encode('ascii')
        (self.source / self.path).write_bytes(source)
        (self.destination / self.path).write_bytes(destination)
        actual_code, report = self.invoke()
        self.assertEqual(actual_code, code)
        result = report['results'][0]
        self.assertTrue(result['source']['json_valid'])
        self.assertTrue(result['destination']['json_valid'])
        self.assertEqual(result['json_structure_equal'], structure_equal)
        self.assertEqual(result['sha256_equal'], source == destination)
        self.assertEqual(result['size_equal'], len(source) == len(destination))
        self.assertEqual(result['status'], 'match' if code == 0 else 'mismatch')

    def test_integer_and_decimal_types_are_distinct(self):
        self.assert_numeric_comparison('1', '1.0', False, 1)
        self.assert_numeric_comparison('1.0', '1', False, 1)

    def test_equal_decimal_values_with_different_scale(self):
        self.assert_numeric_comparison('1.0', '1.00', True, 1)

    def test_boolean_is_not_integer(self):
        self.assert_numeric_comparison('true', '1', False, 1)
        self.assert_numeric_comparison('false', '0', False, 1)

    def test_very_large_integers_are_compared_exactly(self):
        # 1,000 digits are within Python 3.12's default integer conversion limit.
        # The adjacent values would collapse if converted to binary float.
        value = '9' * 1000
        self.assert_numeric_comparison(value, value, True, 0)
        self.assert_numeric_comparison(value, '9' * 999 + '8', False, 1)
        self.assert_numeric_comparison(value, value + '.0', False, 1)

    def test_high_precision_decimals_are_compared_exactly(self):
        value = '0.' + '1234567890' * 20 + '1'
        self.assert_numeric_comparison(value, value, True, 0)
        self.assert_numeric_comparison(value, value[:-1] + '2', False, 1)
        self.assert_numeric_comparison(value, value + '0', True, 1)

    def test_exponent_syntax_retains_decimal_type(self):
        self.assert_numeric_comparison('1e0', '1', False, 1)
        self.assert_numeric_comparison('1e0', '1.00', True, 1)

    def test_multiple_paths_and_forced_queue_validation(self):
        for root in (self.source, self.destination):
            (root / 'generic.json').write_text('{}')
        code, report = self.invoke(['--path', 'generic.json'])
        self.assertEqual(code, 0)
        self.assertEqual(len(report['results']), 2)
        self.assertEqual(self.invoke(['--path', 'generic.json', '--queue'])[0], 2)

    def test_files_are_never_written_by_tool(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*.json')}
        self.invoke()
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*.json')})

if __name__ == '__main__':
    unittest.main()
