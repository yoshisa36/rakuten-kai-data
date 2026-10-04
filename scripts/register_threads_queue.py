#!/usr/bin/env python3
"""Register an explicitly approved batch via GitHub Contents API; never bypass denials."""
import argparse
import base64
import copy
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

REPO = 'yoshisa36/rakuten-kai-data'
PATH = 'threads-queue.json'
JST = timezone(__import__('datetime').timedelta(hours=9))
FIELDS = ('reservationId', 'scheduledAt', 'text', 'postType', 'productName',
          'linkType', 'productUrl', 'approvalStatus')

def validate_batch(batch):
    if not isinstance(batch, dict) or not isinstance(batch.get('posts'), list) or not batch['posts']:
        raise ValueError('Batch must contain a nonempty posts array')
    ids, slots = set(), set()
    for p in batch['posts']:
        if not isinstance(p, dict) or any(k not in p for k in FIELDS):
            raise ValueError('Missing reservation fields')
        if any(not isinstance(p[k], str) for k in FIELDS):
            raise ValueError('Reservation fields must be strings')
        rid = p['reservationId']
        if not re.fullmatch(r'KAI-\d{8}-\d{4}', rid) or 'テスト' in p['postType']:
            raise ValueError('Only production reservation IDs are accepted')
        dt = datetime.fromisoformat(p['scheduledAt'])
        if dt.tzinfo is None or dt.utcoffset() != JST.utcoffset(None):
            raise ValueError('Use explicit +09:00 timestamps')
        if rid != dt.strftime('KAI-%Y%m%d-%H%M') or dt.second or dt.microsecond:
            raise ValueError('Reservation ID and schedule disagree')
        if rid in ids or dt in slots:
            raise ValueError('Duplicate ID or time in batch')
        ids.add(rid); slots.add(dt)
        if p['approvalStatus'] != 'approved' or p.get('status') != '承認待ち':
            raise ValueError('Only explicitly approved, pending import posts accepted')
        if not p['text'].strip() or len(p['text']) > 500:
            raise ValueError('Text must be 1..500 characters before GAS adds links')
        if p['postType'] == '交流':
            if p['linkType'] != 'リンクなし' or p['productUrl']:
                raise ValueError('Conversation posts must have no link')
        elif p['postType'] == '商品':
            u = urlparse(p['productUrl'])
            if p['linkType'] not in ('本文リンク', '返信リンク', 'ROOM誘導') or not p['productName'].strip():
                raise ValueError('Invalid product/link type')
            if u.scheme != 'https' or u.hostname not in ('hb.afl.rakuten.co.jp', 'item.rakuten.co.jp', 'a.r10.to', 'room.rakuten.co.jp') or u.username or u.password:
                raise ValueError('Invalid product URL')
            if p['linkType'] == '本文リンク' and len(p['text'] + '\n\n' + p['productUrl']) > 500:
                raise ValueError('Text plus inline link exceeds 500 characters')
        else:
            raise ValueError('Unknown post type')

def merge(queue, batch, now):
    validate_batch(batch)
    if queue.get('version') != 1 or not isinstance(queue.get('posts'), list):
        raise ValueError('Unsupported queue schema')
    out = copy.deepcopy(queue)
    by_id = {}
    for p in out['posts']:
        if p['reservationId'] in by_id:
            raise ValueError('Existing queue has duplicate IDs')
        by_id[p['reservationId']] = p
    added = []
    for p in batch['posts']:
        old = by_id.get(p['reservationId'])
        if old:
            if any(old.get(k) != p[k] for k in FIELDS):
                raise ValueError('Existing ID has different content: ' + p['reservationId'])
            continue  # Preserve GAS-imported / published status; never reset it.
        dt = datetime.fromisoformat(p['scheduledAt'])
        if dt <= now:
            raise ValueError('Refusing new past-due reservation: ' + p['reservationId'])
        for old in out['posts']:
            if 'テスト' not in old.get('postType', '') and datetime.fromisoformat(old['scheduledAt']) == dt:
                raise ValueError('Production time slot is already occupied')
        out['posts'].append(copy.deepcopy(p)); added.append(p['reservationId'])
    if added:
        out['updatedAt'] = now.astimezone(JST).isoformat(timespec='seconds')
    return out, added

class GitHub:
    def __init__(self, token, branch):
        self.token, self.branch = token, branch
    def request(self, method, suffix, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        req = Request('https://api.github.com/repos/' + REPO + suffix, data=body, method=method,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    def read(self):
        data = self.request('GET', '/contents/' + PATH + '?ref=' + quote(self.branch, safe=''))
        return json.loads(base64.b64decode(data['content'])), data['sha']
    def write(self, queue, sha):
        content = json.dumps(queue, ensure_ascii=False, indent=2) + '\n'
        return self.request('PUT', '/contents/' + PATH, {'branch': self.branch, 'sha': sha,
            'content': base64.b64encode(content.encode()).decode(), 'message': 'Register approved Threads reservations'})

def register(api, batch, now):
    # Only SHA conflicts are retried. Permission/policy failures stop immediately.
    for attempt in range(3):
        queue, sha = api.read()
        merged, added = merge(queue, batch, now)
        if not added:
            return {'status': 'already_registered', 'ids': [p['reservationId'] for p in batch['posts']]}
        try:
            result = api.write(merged, sha)
        except HTTPError as exc:
            if exc.code == 409 and attempt < 2:
                continue
            raise
        actual, actual_sha = api.read()
        indexed = {p['reservationId']: p for p in actual['posts']}
        for expected in batch['posts']:
            stored = indexed.get(expected['reservationId'])
            if stored is None or any(stored.get(k) != expected[k] for k in FIELDS):
                raise RuntimeError('Write verification failed; do not report success')
        return {'status': 'registered_and_verified', 'ids': [p['reservationId'] for p in batch['posts']],
                'commit': result['commit']['sha'], 'content_sha': actual_sha,
                'updatedAt': actual['updatedAt'], 'gas_import': 'not_verified'}
    raise RuntimeError('Concurrent update limit reached')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('batch')
    parser.add_argument('--branch', default='main')
    args = parser.parse_args()
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        raise ValueError('GITHUB_TOKEN is required; never put a token in the JSON or code')
    with open(args.batch, encoding='utf-8') as f:
        batch = json.load(f)
    print(json.dumps(register(GitHub(token, args.branch), batch, datetime.now(timezone.utc)), ensure_ascii=False))

if __name__ == '__main__':
    try:
        main()
    except HTTPError as exc:
        print(json.dumps({'status': 'failed', 'http_status': exc.code,
                          'reason': 'GitHub rejected operation; no alternate credential or endpoint attempted'}), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'reason': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
