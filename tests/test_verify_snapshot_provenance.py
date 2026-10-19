"""No live API calls, secrets, or production data."""
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verify_snapshot_provenance import Evidence, evaluate


class ProvenanceDecisionTests(unittest.TestCase):
    def setUp(self):
        self.good = Evidence(True, False, "same", True, True)

    def check(self, expected, **changes):
        self.assertEqual(expected, evaluate(replace(self.good, **changes))["classification"])

    def test_same_match(self):
        self.check("MATCH")

    def test_forward_change(self):
        self.check("EXPECTED_CHANGE", ancestry="descendant", latest_equal=False)

    def test_forward_unchanged(self):
        self.check("MATCH", ancestry="descendant")

    def test_same_commit_mismatch(self):
        self.check("ALERT", latest_equal=False)

    def test_pinned_baseline_corrupt(self):
        self.check("ALERT", baseline_verified=False, baseline_mismatch=True)

    def test_corruption_overrides_forward_change(self):
        self.check("ALERT", baseline_verified=False, baseline_mismatch=True,
                   ancestry="descendant", latest_equal=False)

    def test_baseline_unknown(self):
        self.check("UNKNOWN", baseline_verified=None, baseline_mismatch=None)

    def test_baseline_contradictory(self):
        self.check("UNKNOWN", baseline_verified=True, baseline_mismatch=True)

    def test_diverged(self):
        self.check("UNKNOWN", ancestry="diverged")

    def test_unknown_ancestry(self):
        self.check("UNKNOWN", ancestry="unknown")

    def test_latest_unavailable(self):
        self.check("UNKNOWN", latest_equal=None)

    def test_head_race(self):
        self.check("UNKNOWN", stable_reads=False)

    def test_invalid_bool(self):
        self.check("UNKNOWN", baseline_verified=1)

    def test_invalid_evidence(self):
        self.assertEqual("UNKNOWN", evaluate({})["classification"])

    def test_immutable_evidence(self):
        with self.assertRaises(AttributeError):
            self.good.ancestry = "diverged"


if __name__ == "__main__":
    unittest.main()
