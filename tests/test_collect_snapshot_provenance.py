"""Offline tests only; no real GitHub requests or credentials."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import collect_snapshot_provenance as collector
import initial_runtime_copy as copy


class Backend:
    def __init__(self):
        self.calls = []

    def request(self, repo, method, suffix):
        self.calls.append((repo, method, suffix))
        return {"mock": True}


class CollectorTests(unittest.TestCase):
    def test_read_only_get(self):
        backend = Backend()
        api = collector.ReadOnly(backend)
        self.assertEqual({"mock": True},
                         api.request(copy.SOURCE, "GET", "/git/ref/heads/main"))
        self.assertEqual(1, len(backend.calls))

    def test_reject_post_without_backend_call(self):
        backend = Backend()
        with self.assertRaises(copy.Stop):
            collector.ReadOnly(backend).request(copy.DESTINATION, "POST", "/git/blobs")
        self.assertEqual([], backend.calls)

    def test_reject_unapproved_repo(self):
        backend = Backend()
        with self.assertRaises(copy.Stop):
            collector.ReadOnly(backend).request("other/repo", "GET", "/git/blobs/abc")
        self.assertEqual([], backend.calls)

    def test_reject_unapproved_endpoint(self):
        backend = Backend()
        with self.assertRaises(copy.Stop):
            collector.ReadOnly(backend).request(copy.SOURCE, "GET", "/actions/secrets")
        self.assertEqual([], backend.calls)

    def test_exact_tree_endpoint_allowed(self):
        backend = Backend()
        suffix = "/git/trees/" + "a" * 40 + "?recursive=1"
        self.assertEqual({"mock": True}, collector.ReadOnly(backend).request(copy.SOURCE, "GET", suffix))

    def test_endpoint_prefix_bypass_rejected(self):
        backend = Backend()
        with self.assertRaises(copy.Stop):
            collector.ReadOnly(backend).request(copy.SOURCE, "GET", "/git/ref/heads/main/extra")
        self.assertEqual([], backend.calls)

    def test_tree_without_recursive_rejected(self):
        backend = Backend()
        with self.assertRaises(copy.Stop):
            collector.ReadOnly(backend).request(copy.SOURCE, "GET", "/git/trees/" + "a" * 40)
        self.assertEqual([], backend.calls)

    def test_unexpected_destination_tree_unknown(self):
        backend = Backend()
        fake = {p: b"{}" for p in copy.PATHS}
        entries = {p: {} for p in copy.PATHS}
        entries.update({"README.md": {}, copy.MANIFEST: {}, "extra.json": {}})
        with patch.object(copy, "head", return_value=collector.BASELINE_SOURCE), \
             patch.object(collector, "_files", side_effect=[(fake, {}), (fake, entries)]):
            result = collector.collect(backend)
        self.assertEqual("UNKNOWN", result["classification"])

    def test_invalid_commit_no_backend_calls(self):
        backend = Backend()
        result = collector.collect(backend, source_commit="main")
        self.assertEqual("UNKNOWN", result["classification"])
        self.assertEqual([], backend.calls)

    def test_unavailable_api_unknown(self):
        backend = Backend()
        with patch.object(copy, "head", side_effect=copy.Stop("unavailable")):
            result = collector.collect(backend)
        self.assertEqual("UNKNOWN", result["classification"])

    def test_no_private_payload_in_error(self):
        class SecretBackend:
            def request(self, *args):
                raise copy.Stop("private_data_should_not_leak")
        result = collector.collect(SecretBackend())
        self.assertNotIn("private_data_should_not_leak", str(result))


if __name__ == "__main__":
    unittest.main()
