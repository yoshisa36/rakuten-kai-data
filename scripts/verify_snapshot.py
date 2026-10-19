#!/usr/bin/env python3
"""Read-only, offline comparison of local JSON snapshots. Linux/POSIX only."""
import argparse
import hashlib
import json
from decimal import Decimal, InvalidOperation
import os
import re
import stat
import sys
from pathlib import PurePosixPath

MAX_BYTES = 8 * 1024 * 1024


class InputError(Exception):
    """Only fixed, non-sensitive error codes may leave the reader/parser."""


def valid_path(value):
    parts = value.split('/')
    return (bool(re.fullmatch(r'[A-Za-z0-9_.\-/]+', value))
            and all(p not in ('', '.', '..') for p in parts)
            and not value.startswith('/') and value.endswith('.json'))


def read_local(root, relative):
    """Walk every component with directory fds and O_NOFOLLOW; never resolve links."""
    if not valid_path(relative):
        raise InputError('invalid_target_path')
    if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise InputError('unsupported_platform')
    # Reject explicit traversal even in the trusted root argument.
    if '..' in root.split('/'):
        raise InputError('invalid_root')
    descriptors = []
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open('/' if root.startswith('/') else '.', directory_flags)
        descriptors.append(fd)
        for component in root.split('/') + relative.split('/')[:-1]:
            if component in ('', '.'):
                continue
            fd = os.open(component, directory_flags, dir_fd=fd)
            descriptors.append(fd)
        fd = os.open(relative.split('/')[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise InputError('not_regular_file')
        if before.st_size > MAX_BYTES:
            raise InputError('file_too_large')
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                raise InputError('file_too_large')
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise InputError('file_changed_during_read')
        return b''.join(chunks)
    except OSError:
        raise InputError('file_unavailable_or_unsafe') from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError('duplicate_json_key')
        result[key] = value
    return result


def reject_constant(value):
    raise InputError('nonstandard_json_number')


def finite_decimal(value):
    result = Decimal(value)
    if not result.is_finite():
        raise InputError('nonfinite_json_number')
    return result


def parse_json(raw):
    try:
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_object,
                          parse_constant=reject_constant, parse_float=finite_decimal)
    except (UnicodeError, ValueError, InvalidOperation, RecursionError):
        raise InputError('invalid_json') from None


def structural_equal(left, right):
    """Object order is irrelevant; numeric types, all values and array order matter."""
    pending = [(left, right)]
    while pending:
        a, b = pending.pop()
        if type(a) is not type(b):
            return False
        if isinstance(a, dict):
            if a.keys() != b.keys():
                return False
            pending.extend((a[key], b[key]) for key in a)
        elif isinstance(a, list):
            if len(a) != len(b):
                return False
            pending.extend(zip(a, b))
        elif a != b:
            return False
    return True


def queue_ids(data):
    if not isinstance(data, dict) or not isinstance(data.get('posts'), list):
        raise InputError('invalid_queue_structure')
    ids = set()
    for post in data['posts']:
        if not isinstance(post, dict) or not isinstance(post.get('reservationId'), str) or not post['reservationId'].strip():
            raise InputError('missing_reservation_id')
        if post['reservationId'] in ids:
            raise InputError('duplicate_reservation_id')
        ids.add(post['reservationId'])
    return ids


def inspect(root, path):
    report = {'read_ok': False, 'json_valid': False}
    try:
        raw = read_local(root, path)
        report.update(read_ok=True, size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        data = parse_json(raw)
        report['json_valid'] = True
        return report, data
    except InputError as exc:
        report['error'] = str(exc)
        return report, None


def compare(source_root, destination_root, path, queue=False):
    source, left = inspect(source_root, path)
    destination, right = inspect(destination_root, path)
    valid = source['json_valid'] and destination['json_valid']
    result = {'path': path, 'source': source, 'destination': destination,
              'size_equal': source['size'] == destination['size'] if source['read_ok'] and destination['read_ok'] else None,
              'sha256_equal': source['sha256'] == destination['sha256'] if source['read_ok'] and destination['read_ok'] else None,
              'json_structure_equal': structural_equal(left, right) if valid else None}
    # Canonical name always validates queue schema, including missing IDs on both sides.
    queue = queue or PurePosixPath(path).name == 'threads-queue.json' or any(
        isinstance(d, dict) and isinstance(d.get('posts'), list) and any(
            isinstance(p, dict) and 'reservationId' in p for p in d['posts']) for d in (left, right))
    if queue:
        queues = {}
        for label, data, report in (('source', left, source), ('destination', right, destination)):
            if not report['json_valid']:
                queues[label] = {'valid': False, 'error': 'json_unavailable'}
                continue
            try:
                ids = queue_ids(data)
                queues[label] = {'valid': True, 'count': len(ids)}
                queues[label + '_ids'] = ids
            except InputError as exc:
                queues[label] = {'valid': False, 'error': str(exc)}
        a, b = queues.pop('source_ids', None), queues.pop('destination_ids', None)
        queues['ids_equal'] = a == b if a is not None and b is not None else None
        if a is not None and b is not None:
            queues.update(missing_in_destination=len(a - b), extra_in_destination=len(b - a))
        result['queue'] = queues
        valid = valid and queues['source']['valid'] and queues['destination']['valid']
    result['status'] = ('invalid' if not valid else 'match' if result['sha256_equal']
                        and result['size_equal'] and result['json_structure_equal'] else 'mismatch')
    return result


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes arbitrary user input. Never include it in logs.
        raise InputError('invalid_arguments')


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--destination-root', required=True)
    parser.add_argument('--path', action='append', required=True, help='Relative JSON path; repeat for multiple files')
    parser.add_argument('--source-commit', required=True, help='Recorded metadata only, not verified provenance')
    parser.add_argument('--queue', action='store_true', help='Require queue validation for every selected file')
    try:
        args = parser.parse_args(argv)
        if not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', args.source_commit):
            raise InputError('invalid_source_commit')
        if any(not valid_path(path) for path in args.path) or len(set(args.path)) != len(args.path):
            raise InputError('invalid_target_path')
        results = [compare(args.source_root, args.destination_root, path, args.queue) for path in args.path]
        code = 2 if any(r['status'] == 'invalid' for r in results) else 1 if any(r['status'] == 'mismatch' for r in results) else 0
        report = {'source_commit': args.source_commit, 'source_commit_verified': False,
                  'results': results, 'status': 'match' if code == 0 else 'invalid' if code == 2 else 'mismatch'}
    except (InputError, ValueError, RecursionError):
        code, report = 2, {'status': 'invalid', 'error': 'invalid_input_or_arguments'}
    print(json.dumps(report, ensure_ascii=True, allow_nan=False, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
