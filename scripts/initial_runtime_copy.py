#!/usr/bin/env python3
"""Initial snapshot only: dry-run by default, approved plan required for writes."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

import verify_snapshot as verifier

SOURCE = 'yoshisa36/rakuten-kai-data'
DESTINATION = 'yoshisa36/rakuten-kai-runtime-data'
PATHS = ('threads-analysis.json', 'threads-queue.json', 'rakuten-affiliate.json',
         'products.json', 'selection-history.json', 'batches/2026-10-05.json')
MANIFEST = 'snapshot-manifest.json'
MAX_RESPONSE = 16 * 1024 * 1024


class Stop(Exception):
    def __init__(self, code):
        self.code = code


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward destination credentials to a redirect host.


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def is_sha(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[0-9a-f]{40}', value))


class GitHub:
    def __init__(self, token):
        if not token:
            raise Stop('destination_token_required')
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, repo, method, suffix, payload=None):
        if repo not in (SOURCE, DESTINATION) or not suffix.startswith('/'):
            raise Stop('invalid_endpoint')
        if method != 'GET' and repo != DESTINATION:
            raise Stop('source_write_forbidden')
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'initial-runtime-copy',
                   'X-GitHub-Api-Version': '2022-11-28'}
        if repo == DESTINATION:
            headers['Authorization'] = 'Bearer ' + self.token
        body = None if payload is None else canonical(payload)
        if body is not None:
            headers['Content-Type'] = 'application/json'
        for attempt in range(3 if method == 'GET' else 1):
            try:
                request = Request('https://api.github.com/repos/' + repo + suffix,
                                  data=body, headers=headers, method=method)
                with self.opener.open(request, timeout=30) as response:
                    raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise Stop('response_too_large')
                return json.loads(raw)
            except HTTPError as exc:
                if method == 'GET' and (exc.code == 429 or 500 <= exc.code <= 599) and attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise Stop('http_' + str(exc.code)) from None
            except (URLError, OSError, TimeoutError):
                if method == 'GET' and attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise Stop('transport_failure' if method == 'GET' else 'write_outcome_unknown') from None
            except (ValueError, UnicodeError):
                raise Stop('invalid_api_response') from None


def head(api, repo):
    value = api.request(repo, 'GET', '/git/ref/heads/main')['object']['sha']
    if not is_sha(value):
        raise Stop('invalid_remote_commit')
    return value


def tree(api, repo, commit):
    c = api.request(repo, 'GET', '/git/commits/' + commit)
    data = api.request(repo, 'GET', '/git/trees/' + c['tree']['sha'] + '?recursive=1')
    if data.get('truncated'):
        raise Stop('truncated_tree')
    entries = {}
    for entry in data['tree']:
        if entry['type'] == 'tree':
            continue
        if entry['path'] in entries or entry['type'] != 'blob' or entry['mode'] != '100644':
            raise Stop('unexpected_tree_entry')
        entries[entry['path']] = entry
    return c, entries


def blob(api, repo, entry):
    data = api.request(repo, 'GET', '/git/blobs/' + entry['sha'])
    if data.get('encoding') != 'base64':
        raise Stop('unexpected_blob_encoding')
    try:
        raw = base64.b64decode(''.join(data['content'].split()), validate=True)
    except (ValueError, TypeError):
        raise Stop('invalid_blob') from None
    if len(raw) != data['size'] or len(raw) > verifier.MAX_BYTES:
        raise Stop('invalid_blob_size')
    # Validate Git's object identity independently of the local SHA-256 report.
    git_sha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    if git_sha != entry['sha']:
        raise Stop('blob_identity_mismatch')
    return raw


def private_destination(api):
    data = api.request(DESTINATION, 'GET', '/')
    if data.get('full_name') != DESTINATION or data.get('private') is not True or data.get('default_branch') != 'main':
        raise Stop('unexpected_destination_configuration')


def source_files(api, commit):
    if not is_sha(commit):
        raise Stop('invalid_source_commit')
    _, entries = tree(api, SOURCE, commit)
    files = {}
    for path in PATHS:
        if path not in entries:
            raise Stop('missing_source_file')
        raw = blob(api, SOURCE, entries[path])
        try:
            data = verifier.parse_json(raw)
            if not isinstance(data, dict):
                raise verifier.InputError('expected_object')
            if path in ('threads-queue.json', 'batches/2026-10-05.json'):
                verifier.queue_ids(data)
        except verifier.InputError:
            raise Stop('invalid_source_json_or_queue') from None
        files[path] = raw
    return files


def file_records(files):
    return [{'path': path, 'size': len(files[path]), 'sha256': digest(files[path])} for path in PATHS]


def prepare(api, source_commit):
    if not is_sha(source_commit):
        raise Stop('invalid_source_commit')
    private_destination(api)
    d0 = head(api, DESTINATION)
    _, entries = tree(api, DESTINATION, d0)
    if set(entries) != {'README.md'}:
        raise Stop('destination_not_readme_only')
    readme = blob(api, DESTINATION, entries['README.md'])
    files = source_files(api, source_commit)
    return {'version': 1, 'source_repository': SOURCE, 'destination_repository': DESTINATION,
            'source_commit': source_commit, 'destination_commit': d0,
            'readme_sha256': digest(readme), 'files': file_records(files)}


def validate_plan(plan, approval):
    keys = {'version', 'source_repository', 'destination_repository', 'source_commit',
            'destination_commit', 'readme_sha256', 'files'}
    if not isinstance(plan, dict) or set(plan) != keys or type(plan['version']) is not int or plan['version'] != 1:
        raise Stop('invalid_plan')
    if plan['source_repository'] != SOURCE or plan['destination_repository'] != DESTINATION:
        raise Stop('invalid_plan_repository')
    if not all(is_sha(plan[k]) for k in ('source_commit', 'destination_commit')):
        raise Stop('invalid_plan_commit')
    def hash_ok(value):
        return isinstance(value, str) and bool(re.fullmatch('[0-9a-f]{64}', value))
    if not hash_ok(plan['readme_sha256']) or not isinstance(plan['files'], list) or len(plan['files']) != len(PATHS):
        raise Stop('invalid_plan_files')
    for record, path in zip(plan['files'], PATHS):
        if not isinstance(record, dict) or set(record) != {'path', 'size', 'sha256'} or record['path'] != path:
            raise Stop('invalid_plan_path')
        if type(record['size']) is not int or not 0 <= record['size'] <= verifier.MAX_BYTES or not hash_ok(record['sha256']):
            raise Stop('invalid_plan_hash_or_size')
    if approval != digest(canonical(plan)):
        raise Stop('approval_mismatch')


def manifest(plan):
    return canonical({'plan': plan, 'plan_sha256': digest(canonical(plan))}) + b'\n'


def verify_saved(api, plan, files, commit):
    _, entries = tree(api, DESTINATION, commit)
    if set(entries) != set(PATHS) | {'README.md', MANIFEST}:
        raise Stop('unexpected_destination_files')
    if digest(blob(api, DESTINATION, entries['README.md'])) != plan['readme_sha256']:
        raise Stop('readme_changed')
    if blob(api, DESTINATION, entries[MANIFEST]) != manifest(plan):
        raise Stop('manifest_mismatch')
    results = []
    with tempfile.TemporaryDirectory(prefix='snapshot-check-') as temp:
        left, right = Path(temp) / 'source', Path(temp) / 'destination'
        for path in PATHS:
            for root, raw in ((left, files[path]), (right, blob(api, DESTINATION, entries[path]))):
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)  # Binary I/O preserves CRLF/BOM exactly on Windows too.
            result = verifier.compare(str(left), str(right), path, queue=path.startswith('batches/'))
            if result['status'] != 'match':
                raise Stop('snapshot_verification_failed')
            results.append(result)
    return results


def apply(api, plan, approval):
    validate_plan(plan, approval)
    # Refuse unsupported verification platforms before creating any remote objects.
    if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise Stop('unsupported_verification_platform')
    private_destination(api)
    files = source_files(api, plan['source_commit'])
    if file_records(files) != plan['files']:
        raise Stop('source_hash_or_size_changed')
    current = head(api, DESTINATION)
    message = 'Initial runtime snapshot ' + approval
    if current != plan['destination_commit']:
        c, _ = tree(api, DESTINATION, current)
        if c['message'] != message or [p['sha'] for p in c['parents']] != [plan['destination_commit']]:
            raise Stop('destination_conflict')
        results = verify_saved(api, plan, files, current)
        if head(api, DESTINATION) != current:
            raise Stop('destination_changed_after_verification')
        return {'status': 'already_applied_and_verified', 'destination_commit': current, 'results': results}
    original, entries = tree(api, DESTINATION, current)
    if set(entries) != {'README.md'} or digest(blob(api, DESTINATION, entries['README.md'])) != plan['readme_sha256']:
        raise Stop('destination_not_readme_only_or_changed')
    elements = []
    for path, raw in list(files.items()) + [(MANIFEST, manifest(plan))]:
        b = api.request(DESTINATION, 'POST', '/git/blobs',
                        {'encoding': 'base64', 'content': base64.b64encode(raw).decode('ascii')})
        elements.append({'path': path, 'type': 'blob', 'mode': '100644', 'sha': b['sha']})
    t = api.request(DESTINATION, 'POST', '/git/trees', {'base_tree': original['tree']['sha'], 'tree': elements})
    c = api.request(DESTINATION, 'POST', '/git/commits',
                    {'message': message, 'tree': t['sha'], 'parents': [current]})
    commit = c['sha']
    if not is_sha(commit):
        raise Stop('invalid_created_commit')
    private_destination(api)
    if head(api, DESTINATION) != current:
        raise Stop('destination_conflict')
    try:
        api.request(DESTINATION, 'PATCH', '/git/refs/heads/main', {'sha': commit, 'force': False})
    except Stop as exc:
        # Never repeat a mutating request. Only resolve a lost transport response.
        if exc.code != 'write_outcome_unknown':
            raise
        if head(api, DESTINATION) != commit:
            raise Stop('ref_update_outcome_unresolved') from None
    if head(api, DESTINATION) != commit:
        raise Stop('destination_changed_after_update')
    results = verify_saved(api, plan, files, commit)
    if head(api, DESTINATION) != commit:
        raise Stop('destination_changed_after_verification')
    return {'status': 'applied_and_verified', 'destination_commit': commit, 'results': results}


def main(argv=None):
    parser = verifier.SafeParser(description=__doc__)
    parser.add_argument('--source-commit')
    parser.add_argument('--apply-plan', help='Local JSON plan, never a URL')
    parser.add_argument('--approve-plan', help='Explicit approval of canonical plan SHA-256')
    try:
        args = parser.parse_args(argv)
        if bool(args.apply_plan) != bool(args.approve_plan) or args.apply_plan and args.source_commit:
            raise Stop('invalid_mode')
        api = GitHub(os.environ.get('RUNTIME_DATA_TOKEN'))
        if args.apply_plan:
            # Reuse secure regular-file reader. Reject links, traversal, huge files.
            absolute = os.path.abspath(args.apply_plan)
            plan = verifier.parse_json(verifier.read_local(os.path.dirname(absolute), os.path.basename(absolute)))
            if isinstance(plan, dict) and set(plan) == {'status', 'plan', 'plan_sha256', 'source_commit_verified_by_snapshot_cli'}:
                if plan['status'] != 'dry_run' or plan['plan_sha256'] != args.approve_plan:
                    raise Stop('approval_mismatch')
                plan = plan['plan']
            report = apply(api, plan, args.approve_plan)
        else:
            plan = prepare(api, args.source_commit)
            report = {'status': 'dry_run', 'plan': plan, 'plan_sha256': digest(canonical(plan)),
                      'source_commit_verified_by_snapshot_cli': False}
        print(json.dumps(report, sort_keys=True))
        return 0
    except (Stop, verifier.InputError, OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        code = exc.code if isinstance(exc, Stop) else 'invalid_input_or_response'
        print(json.dumps({'status': 'stopped', 'error': code}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
