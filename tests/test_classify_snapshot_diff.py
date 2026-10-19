"""Offline tests for snapshot classification; synthetic metadata only."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from classify_snapshot_diff import classify, EXPECTED_PATHS

A = "a" * 40
B = "b" * 40


def item(status="match", queue=None):
    value = {"path": "threads-queue.json", "status": status,
             "sha256_equal": status == "match",
             "size_equal": status == "match",
             "json_structure_equal": status == "match",
             "source": {"json_valid": status != "invalid"},
             "destination": {"json_valid": status != "invalid"}}
    if queue is not None:
        value["queue"] = queue
    return value


def report(*items):
    if not items:
        return {"results": []}
    selected = {value["path"]: value for value in items}
    complete = [selected.get(path, dict(item(), path=path)) for path in sorted(EXPECTED_PATHS)]
    if len(items) > len(selected):
        complete.append(items[-1])
    return {"results": complete}


class SnapshotDiffClassifierTests(unittest.TestCase):
    def check(self, expected, data, source=A, snapshot=A):
        self.assertEqual(expected, classify(data, source, snapshot)["classification"])

    def test_same_commit_match(self):
        self.check("MATCH", report(item()))

    def test_same_commit_mismatch_alert(self):
        self.check("ALERT", report(item("mismatch")))

    def test_new_commit_mismatch_expected_change(self):
        self.check("EXPECTED_CHANGE", report(item("mismatch")), B, A)

    def test_new_commit_match_unknown(self):
        self.check("UNKNOWN", report(item()), B, A)

    def test_invalid_data_unknown(self):
        self.check("UNKNOWN", report(item("invalid")))

    def test_no_results_unknown(self):
        self.check("UNKNOWN", report())

    def test_bad_sha_unknown(self):
        self.check("UNKNOWN", report(item()), "main", A)

    def test_duplicate_path_unknown(self):
        self.check("UNKNOWN", report(item(), item()))

    def test_inconsistent_match_unknown(self):
        data = item()
        data["sha256_equal"] = False
        self.check("UNKNOWN", report(data))

    def test_invalid_mismatch_unknown(self):
        data = item("mismatch")
        data["destination"]["json_valid"] = False
        self.check("UNKNOWN", report(data))

    def test_queue_id_drift_same_commit_alert(self):
        queue = {"source": {"valid": True}, "destination": {"valid": True},
                 "ids_equal": False}
        self.check("ALERT", report(item("mismatch", queue)))

    def test_queue_invalid_unknown(self):
        queue = {"source": {"valid": True}, "destination": {"valid": False},
                 "ids_equal": None}
        self.check("UNKNOWN", report(item("invalid", queue)))

    def test_missing_source_object_unknown(self):
        data = item("mismatch")
        data["source"] = None
        self.check("UNKNOWN", report(data))

    def test_missing_path_unknown(self):
        self.check("UNKNOWN", {"results": [item()]})

    def test_unexpected_path_unknown(self):
        data = report(item())
        data["results"][0]["path"] = "unapproved.json"
        self.check("UNKNOWN", data)

    def test_inconsistent_mismatch_unknown(self):
        data = item("mismatch")
        data["sha256_equal"] = True
        self.check("UNKNOWN", report(data))

    def test_does_not_mutate_input(self):
        data = report(item())
        original = repr(data)
        classify(data, A, A)
        self.assertEqual(original, repr(data))


if __name__ == "__main__":
    unittest.main()
