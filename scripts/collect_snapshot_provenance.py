#!/usr/bin/env python3
"""Read-only provenance collector with an injected, mockable GitHub GET interface.

No credentials, network client, CLI, writes, or scheduled execution are provided.
The adapter must expose request(repo, method, suffix) and permit GET only.
"""
import hashlib
import re

import initial_runtime_copy as copy
import verify_snapshot as verifier
from verify_snapshot_provenance import Evidence, evaluate

BASELINE_SOURCE = "2e28969c5a7119fb6de83664cb835c989c55dcfe"
BASELINE_DESTINATION = "15a48986ef3565ba7600440ea8059783928b4e2a"


class ReadOnly:
    def __init__(self, backend):
        self.backend = backend

    def request(self, repo, method, suffix):
        if method != "GET" or repo not in (copy.SOURCE, copy.DESTINATION):
            raise copy.Stop("read_only_violation")
        if not (suffix == "/git/ref/heads/main" or re.fullmatch(
                r"/git/(?:commits/[0-9a-f]{40}|trees/[0-9a-f]{40}\?recursive=1|blobs/[0-9a-f]{40})",
                suffix)):
            raise copy.Stop("endpoint_not_allowed")
        return self.backend.request(repo, method, suffix)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _files(api, repo, commit):
    _, entries = copy.tree(api, repo, commit)
    if not set(copy.PATHS).issubset(entries):
        raise copy.Stop("missing_snapshot_path")
    result = {}
    for path in copy.PATHS:
        raw = copy.blob(api, repo, entries[path])
        data = verifier.parse_json(raw)
        if not isinstance(data, dict):
            raise copy.Stop("non_object_json")
        if path in ("threads-queue.json", "batches/2026-10-05.json"):
            verifier.queue_ids(data)
        result[path] = raw
    return result, entries


def ancestry(api, baseline, head, max_commits=256):
    """Bounded proof of ancestry via pinned Git commit parent links.

    Missing, malformed, cyclic, or overly deep history is unknown, never
    interpreted as a proven forward update.
    """
    if (type(max_commits) is not int or not 1 <= max_commits <= 4096
            or not copy.is_sha(baseline) or not copy.is_sha(head)):
        raise copy.Stop("invalid_ancestry_commit")
    if head == baseline:
        return "same"
    visited = set()
    pending = [head]
    while pending:
        if len(visited) >= max_commits:
            return "unknown"
        current = pending.pop()
        if current == baseline:
            return "descendant"
        if current in visited:
            continue
        visited.add(current)
        obj = copy.validate_commit(
            api.request(copy.SOURCE, "GET", "/git/commits/" + current),
            expected=current)
        raw_parents = copy.required(obj, "parents", list)
        if len(raw_parents) > 16:
            return "unknown"
        parents = [copy.required_sha(p) for p in raw_parents]
        pending.extend(p for p in parents if p not in visited)
        if len(pending) > max_commits * 16:
            return "unknown"
    return "unknown"


def collect(backend, source_commit=BASELINE_SOURCE,
            destination_commit=BASELINE_DESTINATION):
    """Return metadata-only classification; do not disclose private contents."""
    try:
        if not copy.is_sha(source_commit) or not copy.is_sha(destination_commit):
            raise copy.Stop("invalid_pinned_commit")
        api = ReadOnly(backend)
        before = copy.head(api, copy.SOURCE)
        source, _ = _files(api, copy.SOURCE, source_commit)
        destination, entries = _files(api, copy.DESTINATION, destination_commit)
        if set(entries) != set(copy.PATHS) | {"README.md", copy.MANIFEST}:
            raise copy.Stop("unexpected_destination_tree")
        manifest_raw = copy.blob(api, copy.DESTINATION, entries[copy.MANIFEST])
        manifest = verifier.parse_json(manifest_raw)
        if not isinstance(manifest, dict) or set(manifest) != {"plan", "plan_sha256"}:
            raise copy.Stop("invalid_manifest")
        plan = manifest["plan"]
        copy.validate_plan(plan, manifest["plan_sha256"])
        if manifest_raw != copy.manifest(plan):
            raise copy.Stop("noncanonical_manifest")
        if plan["source_commit"] != source_commit:
            raise copy.Stop("source_commit_mismatch")
        # The snapshot commit must be a direct child of the pre-copy
        # destination commit recorded in the manifest. Fetch both immutable
        # commits independently; do not infer ancestry from SHA formatting.
        completed = copy.validate_commit(
            api.request(copy.DESTINATION, "GET", "/git/commits/" + destination_commit),
            expected=destination_commit)
        if copy.required(completed, "message", str) != "Initial runtime snapshot " + manifest["plan_sha256"]:
            raise copy.Stop("snapshot_commit_message_mismatch")
        parents = copy.required(completed, "parents", list)
        if len(parents) != 1 or copy.required_sha(parents[0]) != plan["destination_commit"]:
            raise copy.Stop("destination_parent_mismatch")
        copy.validate_commit(
            api.request(copy.DESTINATION, "GET", "/git/commits/" + plan["destination_commit"]),
            expected=plan["destination_commit"])
        # Independently verify that the pre-copy parent contained the same
        # README as the completed snapshot; a matching child hash alone does
        # not prove the original README was preserved.
        _, parent_entries = copy.tree(api, copy.DESTINATION, plan["destination_commit"])
        if set(parent_entries) != {"README.md"}:
            raise copy.Stop("unexpected_precopy_tree")
        parent_readme = copy.blob(api, copy.DESTINATION, parent_entries["README.md"])
        if _hash(parent_readme) != plan["readme_sha256"]:
            raise copy.Stop("precopy_readme_mismatch")
        if _hash(copy.blob(api, copy.DESTINATION, entries["README.md"])) != plan["readme_sha256"]:
            raise copy.Stop("readme_mismatch")
        for record in plan["files"]:
            raw = source[record["path"]]
            if len(raw) != record["size"] or _hash(raw) != record["sha256"]:
                raise copy.Stop("manifest_file_mismatch")
        # A baseline mismatch is a confirmed corruption only when every input
        # above is independently readable and validated.
        baseline_equal = all(source[p] == destination[p] for p in copy.PATHS)
        after = copy.head(api, copy.SOURCE)
        if before != after:
            return evaluate(Evidence(True, False, "unknown", None, False))
        if not baseline_equal:
            evidence = Evidence(False, True, "unknown", None, True)
            return evaluate(evidence)
        relation = ancestry(api, source_commit, before)
        if relation == "unknown":
            return evaluate(Evidence(True, False, relation, None, True))
        if relation == "same":
            return evaluate(Evidence(True, False, relation, True, True))
        latest, _ = _files(api, copy.SOURCE, before)
        final_head = copy.head(api, copy.SOURCE)
        if final_head != before:
            return evaluate(Evidence(True, False, "unknown", None, False))
        latest_equal = all(latest[p] == source[p] for p in copy.PATHS)
        return evaluate(Evidence(True, False, relation, latest_equal, True))
    except (copy.Stop, verifier.InputError, ValueError, TypeError, KeyError,
            AttributeError, RecursionError):
        return {"classification": "UNKNOWN", "reason": "unavailable_or_invalid_evidence"}
