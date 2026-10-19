#!/usr/bin/env python3
"""Offline, read-only classification of snapshot comparisons.

Input is an explicit, trusted comparison report and two commit identifiers.
This module does not fetch GitHub state or claim to verify provenance.
"""
import re

_SHA = re.compile(r"[0-9a-f]{40}\Z")
LABELS = ("MATCH", "EXPECTED_CHANGE", "ALERT", "UNKNOWN")


def classify(comparison, current_source_commit, snapshot_source_commit):
    """Return a conservative classification without leaking payloads or IDs.

    Both commits must come from independently verified provenance; a snapshot
    manifest's self-reported SHA alone is not proof of origin.
    """
    if not (isinstance(current_source_commit, str)
            and isinstance(snapshot_source_commit, str)
            and _SHA.fullmatch(current_source_commit)
            and _SHA.fullmatch(snapshot_source_commit)):
        return {"classification": "UNKNOWN", "reason": "unverified_commit_input"}
    if not isinstance(comparison, dict):
        return {"classification": "UNKNOWN", "reason": "invalid_comparison"}
    results = comparison.get("results")
    if not isinstance(results, list) or not results:
        return {"classification": "UNKNOWN", "reason": "missing_results"}
    paths = set()
    statuses = []
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return {"classification": "UNKNOWN", "reason": "invalid_result"}
        path = item["path"]
        if not path or path in paths:
            return {"classification": "UNKNOWN", "reason": "duplicate_or_empty_path"}
        paths.add(path)
        status = item.get("status")
        if status not in ("match", "mismatch", "invalid"):
            return {"classification": "UNKNOWN", "reason": "invalid_result_status"}
        statuses.append(status)
        if status == "match":
            if not all(item.get(key) is True for key in
                       ("sha256_equal", "size_equal", "json_structure_equal")):
                return {"classification": "UNKNOWN", "reason": "inconsistent_match"}
        if status == "mismatch":
            if not (item.get("source", {}).get("json_valid") is True
                    and item.get("destination", {}).get("json_valid") is True):
                return {"classification": "UNKNOWN", "reason": "invalid_mismatch"}
        queue = item.get("queue")
        if queue is not None:
            if not isinstance(queue, dict) or not all(
                    isinstance(queue.get(side), dict)
                    and queue[side].get("valid") is True
                    for side in ("source", "destination")):
                return {"classification": "UNKNOWN", "reason": "queue_unavailable"}
            if queue.get("ids_equal") is False and current_source_commit == snapshot_source_commit:
                return {"classification": "ALERT", "reason": "same_commit_queue_ids_differ"}
    if "invalid" in statuses:
        return {"classification": "UNKNOWN", "reason": "invalid_or_unavailable_data"}
    same_commit = current_source_commit == snapshot_source_commit
    if same_commit:
        if all(status == "match" for status in statuses):
            return {"classification": "MATCH", "reason": "same_commit_identical"}
        return {"classification": "ALERT", "reason": "same_commit_content_mismatch"}
    if "mismatch" in statuses:
        return {"classification": "EXPECTED_CHANGE", "reason": "different_commit_snapshot_stale"}
    return {"classification": "UNKNOWN", "reason": "different_commit_same_content"}
