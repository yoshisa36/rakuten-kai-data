#!/usr/bin/env python3
"""Pure, offline snapshot provenance evidence evaluator.

This module performs no network requests, writes, or credential access.
Evidence must be independently established by a future read-only adapter.
"""
from dataclasses import dataclass
from typing import Literal

Status = Literal["MATCH", "EXPECTED_CHANGE", "ALERT", "UNKNOWN"]


@dataclass(frozen=True)
class Evidence:
    # True only after independent pinned-commit blob/manifest/queue validation.
    baseline_verified: bool | None
    # True only if immutable baseline comparison proves corruption.
    baseline_mismatch: bool | None
    # "same", "descendant", "diverged", or "unknown", verified independently.
    ancestry: str
    # Equality of current pinned HEAD's six JSON files to baseline.
    latest_equal: bool | None
    # False if HEAD moved during verification or a read was incomplete.
    stable_reads: bool


def evaluate(evidence: Evidence) -> dict[str, str]:
    """Fail closed on absent, contradictory, or unverified evidence."""
    if not isinstance(evidence, Evidence):
        return {"classification": "UNKNOWN", "reason": "invalid_evidence"}
    if type(evidence.stable_reads) is not bool:
        return {"classification": "UNKNOWN", "reason": "invalid_stability_flag"}
    if not evidence.stable_reads:
        return {"classification": "UNKNOWN", "reason": "unstable_or_incomplete_reads"}
    if (type(evidence.baseline_verified) not in (bool, type(None))
            or type(evidence.baseline_mismatch) not in (bool, type(None))
            or type(evidence.latest_equal) not in (bool, type(None))
            or evidence.ancestry not in ("same", "descendant", "diverged", "unknown")):
        return {"classification": "UNKNOWN", "reason": "invalid_evidence_values"}
    if evidence.baseline_verified is True and evidence.baseline_mismatch is True:
        return {"classification": "UNKNOWN", "reason": "contradictory_baseline"}
    if evidence.baseline_mismatch is True and evidence.baseline_verified is False:
        return {"classification": "ALERT", "reason": "pinned_baseline_corruption"}
    if evidence.baseline_verified is not True or evidence.baseline_mismatch is not False:
        return {"classification": "UNKNOWN", "reason": "baseline_not_verified"}
    if evidence.ancestry not in ("same", "descendant"):
        return {"classification": "UNKNOWN", "reason": "unverified_ancestry"}
    if evidence.latest_equal is None:
        return {"classification": "UNKNOWN", "reason": "latest_content_unavailable"}
    if evidence.ancestry == "same" and evidence.latest_equal is False:
        return {"classification": "ALERT", "reason": "same_commit_changed_content"}
    if evidence.latest_equal is True:
        return {"classification": "MATCH", "reason": "baseline_intact_content_equal"}
    return {"classification": "EXPECTED_CHANGE", "reason": "verified_descendant_content_change"}
