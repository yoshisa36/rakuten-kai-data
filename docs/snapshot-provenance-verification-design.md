# STEP 23 — Read-only snapshot provenance verification design

Status: **design plus offline evidence-decision prototype and injected GET-only baseline collector**. No live GitHub client, credentials, or execution workflow. This document does not authorize running against production, merging another PR, changing GAS, scheduling a job, or enabling replies.

## Scope and trust boundaries

- Live authoritative source: public `yoshisa36/rakuten-kai-data`, branch `main`.
- One-time private destination: `yoshisa36/rakuten-kai-runtime-data`, containing six copied JSON files and `snapshot-manifest.json`.
- Initial copy's previously recorded source commit: `2e28969c5a7119fb6de83664cb835c989c55dcfe`; destination commit: `15a48986ef3565ba7600440ea8059783928b4e2a`.
- Neither a self-declared manifest commit nor a 40-character SHA string proves provenance. Independently fetch the immutable source commit from GitHub, pin the private destination commit, and verify exact blobs.
- The existing `verify_snapshot.py --source-commit` **records metadata only**. Do not treat its `source_commit_verified: false` as an attestation.
- No new long-lived PAT or token in repository files/logs. Private reads, if necessary, use minimum read-only authorization supplied by the user; do not reuse the initial-copy token; its revocation has not been confirmed.

## Proposed workflow (baseline collector is mockable; bounded offline ancestry proof is implemented; live adapter is not implemented)

1. **Pin immutable inputs:** query the public source commit by its exact SHA and private destination by its exact SHA; record the public main HEAD separately. A changing HEAD is not an immutable baseline.
2. **Verify manifest:** parse with duplicate-key rejection and a strict schema; require six unique approved paths, expected source/destination repository identities and source commit; verify each declared SHA256/byte size against GitHub blobs at the pinned commits. Treat unknown/missing manifest fields according to a documented schema, not as implicit success.
3. **Verify baseline content:** retrieve the six files at the pinned public source commit and at the pinned private destination commit, enforce file-size bounds and JSON validation, compare SHA256, size, JSON structure, and queue reservation IDs. Missing files, ambiguous trees, duplicate IDs, invalid JSON, or unexpected symlinks => UNKNOWN, never MATCH.
4. **Determine source advancement:** independently fetch current public main HEAD; check via GitHub compare/ancestry whether the baseline source commit is an ancestor of current HEAD. Do not equate different SHAs with forward progress. Divergent/rebased/unknown ancestry => UNKNOWN.
5. **Compare latest source with baseline separately:** classify a changed current source only after baseline is verified. A valid forward update may be reported as `EXPECTED_CHANGE` **with baseline integrity separately proven**. If current HEAD changes during retrieval, retry against pinned HEAD or mark UNKNOWN.
6. **Emit metadata-only result:** report status, commit IDs, file path identifiers, mismatch counts, evidence booleans and reason codes. Never output private JSON payloads, reservation IDs, secrets, or private repository contents to public Actions logs/artifacts.

## Decision table

| Baseline integrity | Current source ancestry | Latest source vs baseline | Classification |
| --- | --- | --- | --- |
| verified equal | same commit | equal | MATCH |
| verified equal | proven descendant | differs | EXPECTED_CHANGE |
| verified equal | proven descendant | equal | MATCH (unchanged contents) |
| proven mismatch at pinned baseline | any | any | ALERT |
| unknown / unavailable / invalid | any | any | UNKNOWN |
| verified equal | unknown / divergent | any | UNKNOWN |

A genuine source update must never conceal a corrupted private baseline. Conversely, a stale but intact snapshot must not automatically trigger a corruption alert.

## Safety and acceptance gates

- Read-only GET requests only; no Git Data write endpoints, no push, no GAS calls, no Threads calls, no scheduled triggers, no automatic repairs.
- Develop as separate Draft PR with mocked GitHub responses and no live credentials in CI.
- Test: same pinned commit; verified forward update; content unchanged across forward update; rewritten/diverged history; corrupted baseline; missing path; duplicate manifest path; invalid manifest; invalid queue IDs; truncated blob; race between HEAD reads; unauthorized/private 404; rate limit; timeout; no token leakage.
- Run offline tests and inspect code diff. Any later real-data read requires explicit review of permissions and logging. **Do not merge without user approval.**
- Keep PR #5 independent and Draft until separately approved. This design PR does not change its state.

## Current limitations

This includes a GET-only mockable baseline collector, not evidence that the live/private snapshots currently match. The collector traverses pinned commit parents up to 256 commits; missing, divergent, or deeper history remains UNKNOWN. It compares current pinned HEAD contents only after proving ancestry, and rechecks HEAD stability. No real GitHub connection has been made. The original one-time copy verification was performed at creation time, but the current state of either repository is not asserted here.


## Independent approval-hash trust anchor (pre-connection review)

The collector validates the manifest's `plan_sha256` against the canonical plan
and checks that the immutable snapshot commit message includes that hash.
It also pins the source, pre-copy destination, and completed snapshot commit
SHAs independently. These checks establish consistency with the pinned
commit history, but **do not independently pin the approval hash**.

Before introducing a real-data adapter, compare the original operator-approved
dry-run `plan_sha256` with the manifest and commit message using an explicitly
authorized, read-only evidence review. Record the confirmed value and evidence
source in this design, then decide whether to add a separate constant and
regression tests. Do not infer the approved value solely from the manifest being
validated, and do not claim operator approval based on a matching computed hash.

This review is a release gate, not authorization to access the private
repository, deploy a collector, merge this Draft PR, or enable automatic replies.
