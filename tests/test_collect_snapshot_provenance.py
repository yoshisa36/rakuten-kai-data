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

    def test_pinned_baseline_mismatch_alert(self):
        source = {p: b"{}" for p in copy.PATHS}
        destination = dict(source)
        destination[copy.PATHS[0]] = b'{"changed":true}'
        entries = {p: {} for p in copy.PATHS}
        entries.update({"README.md": {}, copy.MANIFEST: {}})
        plan = {"destination_commit": "a" * 40,
                "source_commit": collector.BASELINE_SOURCE,
                "readme_sha256": collector._hash(b"readme"),
                "files": [{"path": p, "size": len(source[p]),
                           "sha256": collector._hash(source[p])} for p in copy.PATHS]}
        with patch.object(copy, "head", return_value=collector.BASELINE_SOURCE), \
             patch.object(collector, "_files", side_effect=[(source, {}), (destination, entries)]), \
             patch.object(copy, "blob", side_effect=[b"manifest", b"readme", b"readme"]), \
             patch.object(copy, "tree", return_value=({}, {"README.md": {}})), \
             patch.object(collector.verifier, "parse_json", return_value={"plan": plan, "plan_sha256": "approved"}), \
             patch.object(copy, "validate_plan", return_value=None), \
             patch.object(copy, "manifest", return_value=b"manifest"), \
             patch.object(copy, "validate_commit", return_value={"parents": [{"sha": "a" * 40}], "message": "Initial runtime snapshot " + ("approved" if "plan_sha256" not in locals() else collector._hash(copy.canonical(plan)))}):
            result = collector.collect(Backend())
        self.assertEqual("ALERT", result["classification"])

    def test_head_change_suppresses_alert(self):
        source = {p: b"{}" for p in copy.PATHS}
        destination = dict(source)
        destination[copy.PATHS[0]] = b'{"changed":true}'
        entries = {p: {} for p in copy.PATHS}
        entries.update({"README.md": {}, copy.MANIFEST: {}})
        plan = {"destination_commit": "a" * 40,
                "source_commit": collector.BASELINE_SOURCE,
                "readme_sha256": collector._hash(b"readme"),
                "files": [{"path": p, "size": len(source[p]),
                           "sha256": collector._hash(source[p])} for p in copy.PATHS]}
        with patch.object(copy, "head", side_effect=[collector.BASELINE_SOURCE, "f" * 40]), \
             patch.object(collector, "_files", side_effect=[(source, {}), (destination, entries)]), \
             patch.object(copy, "blob", side_effect=[b"manifest", b"readme", b"readme"]), \
             patch.object(copy, "tree", return_value=({}, {"README.md": {}})), \
             patch.object(collector.verifier, "parse_json", return_value={"plan": plan, "plan_sha256": "approved"}), \
             patch.object(copy, "validate_plan", return_value=None), \
             patch.object(copy, "manifest", return_value=b"manifest"), \
             patch.object(copy, "validate_commit", return_value={"parents": [{"sha": "a" * 40}], "message": "Initial runtime snapshot " + ("approved" if "plan_sha256" not in locals() else collector._hash(copy.canonical(plan)))}):
            result = collector.collect(Backend())
        self.assertEqual("UNKNOWN", result["classification"])

    def test_real_manifest_validator_rejects_forged_approval(self):
        """Exercise the real manifest validator, not a patched acceptance."""
        source = {p: b"{}" for p in copy.PATHS}
        entries = {p: {} for p in copy.PATHS}
        entries.update({"README.md": {}, copy.MANIFEST: {}})
        plan = {
            "version": 1, "source_repository": copy.SOURCE,
            "destination_repository": copy.DESTINATION,
            "source_commit": collector.BASELINE_SOURCE,
            "destination_commit": "a" * 40,
            "readme_sha256": collector._hash(b"readme"),
            "files": [{"path": p, "size": 2, "sha256": collector._hash(b"{}")}
                      for p in copy.PATHS],
        }
        import json
        manifest = json.dumps({"plan": plan, "plan_sha256": "0" * 64}).encode()
        with patch.object(copy, "head", return_value=collector.BASELINE_SOURCE), \
             patch.object(collector, "_files", side_effect=[(source, {}), (source, entries)]), \
             patch.object(copy, "blob", side_effect=[manifest]):
            result = collector.collect(Backend())
        self.assertEqual("UNKNOWN", result["classification"])

    def test_manifest_duplicate_keys_unknown(self):
        source = {p: b"{}" for p in copy.PATHS}
        entries = {p: {} for p in copy.PATHS}
        entries.update({"README.md": {}, copy.MANIFEST: {}})
        manifest = b'{"plan":{},"plan":{},"plan_sha256":"bad"}'
        with patch.object(copy, "head", return_value=collector.BASELINE_SOURCE), \
             patch.object(collector, "_files", side_effect=[(source, {}), (source, entries)]), \
             patch.object(copy, "blob", side_effect=[manifest]):
            result = collector.collect(Backend())
        self.assertEqual("UNKNOWN", result["classification"])

    def test_valid_manifest_same_commit_match(self):
        """Real plan validation, real JSON parser, and final MATCH decision."""
        import json
        source = {p: b"{}" for p in copy.PATHS}
        entries = {p: {} for p in copy.PATHS}
        entries.update({"README.md": {}, copy.MANIFEST: {}})
        plan = {
            "version": 1, "source_repository": copy.SOURCE,
            "destination_repository": copy.DESTINATION,
            "source_commit": collector.BASELINE_SOURCE,
            "destination_commit": "a" * 40,
            "readme_sha256": collector._hash(b"readme"),
            "files": [{"path": p, "size": len(source[p]),
                       "sha256": collector._hash(source[p])} for p in copy.PATHS],
        }
        approval = collector._hash(copy.canonical(plan))
        manifest = copy.manifest(plan)
        with patch.object(copy, "head", return_value=collector.BASELINE_SOURCE), \
             patch.object(collector, "_files", side_effect=[(source, {}), (source, entries)]), \
             patch.object(copy, "blob", side_effect=[manifest, b"readme", b"readme"]), \
             patch.object(copy, "tree", return_value=({}, {"README.md": {}})), \
             patch.object(copy, "validate_commit", return_value={"parents": [{"sha": "a" * 40}], "message": "Initial runtime snapshot " + ("approved" if "plan_sha256" not in locals() else collector._hash(copy.canonical(plan)))}):
            result = collector.collect(Backend())
        self.assertEqual({"classification": "MATCH",
                          "reason": "baseline_intact_content_equal"}, result)

    def test_valid_manifest_corrupted_destination_alert(self):
        """A valid manifest cannot hide a changed private baseline."""
        import json
        source = {p: b"{}" for p in copy.PATHS}
        destination = dict(source)
        destination[copy.PATHS[0]] = b'{"tampered":true}'
        entries = {p: {} for p in copy.PATHS}
        entries.update({"README.md": {}, copy.MANIFEST: {}})
        plan = {
            "version": 1, "source_repository": copy.SOURCE,
            "destination_repository": copy.DESTINATION,
            "source_commit": collector.BASELINE_SOURCE,
            "destination_commit": "a" * 40,
            "readme_sha256": collector._hash(b"readme"),
            "files": [{"path": p, "size": len(source[p]),
                       "sha256": collector._hash(source[p])} for p in copy.PATHS],
        }
        approval = collector._hash(copy.canonical(plan))
        manifest = copy.manifest(plan)
        with patch.object(copy, "head", return_value=collector.BASELINE_SOURCE), \
             patch.object(collector, "_files", side_effect=[(source, {}), (destination, entries)]), \
             patch.object(copy, "blob", side_effect=[manifest, b"readme", b"readme"]), \
             patch.object(copy, "tree", return_value=({}, {"README.md": {}})), \
             patch.object(copy, "validate_commit", return_value={"parents": [{"sha": "a" * 40}], "message": "Initial runtime snapshot " + ("approved" if "plan_sha256" not in locals() else collector._hash(copy.canonical(plan)))}):
            result = collector.collect(Backend())
        self.assertEqual("ALERT", result["classification"])

    def test_destination_parent_mismatch_unknown(self):
        """Reject a valid plan if the completed snapshot is not its child."""
        import json
        source = {p: b"{}" for p in copy.PATHS}
        entries = {p: {} for p in copy.PATHS}
        entries.update({"README.md": {}, copy.MANIFEST: {}})
        plan = {
            "version": 1, "source_repository": copy.SOURCE,
            "destination_repository": copy.DESTINATION,
            "source_commit": collector.BASELINE_SOURCE,
            "destination_commit": "a" * 40,
            "readme_sha256": collector._hash(b"readme"),
            "files": [{"path": p, "size": 2, "sha256": collector._hash(b"{}")}
                      for p in copy.PATHS],
        }
        manifest = json.dumps({"plan": plan, "plan_sha256":
                               collector._hash(copy.canonical(plan))}).encode()
        with patch.object(copy, "head", return_value=collector.BASELINE_SOURCE), \
             patch.object(collector, "_files", side_effect=[(source, {}), (source, entries)]), \
             patch.object(copy, "blob", side_effect=[manifest]), \
             patch.object(copy, "validate_commit", return_value={"parents": [{"sha": "b" * 40}]}):
            result = collector.collect(Backend())
        self.assertEqual("UNKNOWN", result["classification"])

    def test_destination_merge_commit_rejected(self):
        """Snapshot must have exactly one pre-copy parent, never a merge."""
        import json
        source = {p: b"{}" for p in copy.PATHS}
        entries = {p: {} for p in copy.PATHS}
        entries.update({"README.md": {}, copy.MANIFEST: {}})
        plan = {
            "version": 1, "source_repository": copy.SOURCE,
            "destination_repository": copy.DESTINATION,
            "source_commit": collector.BASELINE_SOURCE,
            "destination_commit": "a" * 40,
            "readme_sha256": collector._hash(b"readme"),
            "files": [{"path": p, "size": 2, "sha256": collector._hash(b"{}")}
                      for p in copy.PATHS],
        }
        manifest = json.dumps({"plan": plan, "plan_sha256":
                               collector._hash(copy.canonical(plan))}).encode()
        with patch.object(copy, "head", return_value=collector.BASELINE_SOURCE), \
             patch.object(collector, "_files", side_effect=[(source, {}), (source, entries)]), \
             patch.object(copy, "blob", side_effect=[manifest]), \
             patch.object(copy, "validate_commit", return_value={
                 "parents": [{"sha": "a" * 40}, {"sha": "b" * 40}], "message": "Initial runtime snapshot " + collector._hash(copy.canonical(plan))}):
            result = collector.collect(Backend())
        self.assertEqual("UNKNOWN", result["classification"])

    def test_precopy_readme_mismatch_unknown(self):
        """A valid child README cannot conceal a different parent README."""
        import json
        source = {p: b"{}" for p in copy.PATHS}
        entries = {p: {} for p in copy.PATHS}
        entries.update({"README.md": {}, copy.MANIFEST: {}})
        plan = {
            "version": 1, "source_repository": copy.SOURCE,
            "destination_repository": copy.DESTINATION,
            "source_commit": collector.BASELINE_SOURCE,
            "destination_commit": "a" * 40,
            "readme_sha256": collector._hash(b"readme"),
            "files": [{"path": p, "size": 2, "sha256": collector._hash(b"{}")}
                      for p in copy.PATHS],
        }
        manifest = json.dumps({"plan": plan, "plan_sha256":
                               collector._hash(copy.canonical(plan))}).encode()
        with patch.object(copy, "head", return_value=collector.BASELINE_SOURCE), \
             patch.object(collector, "_files", side_effect=[(source, {}), (source, entries)]), \
             patch.object(copy, "blob", side_effect=[manifest, b"different"]), \
             patch.object(copy, "tree", return_value=({}, {"README.md": {}})), \
             patch.object(copy, "validate_commit", return_value={
                 "parents": [{"sha": "a" * 40}], "message": "Initial runtime snapshot " + collector._hash(copy.canonical(plan))}):
            result = collector.collect(Backend())
        self.assertEqual("UNKNOWN", result["classification"])

    def test_noncanonical_manifest_unknown(self):
        """Same parsed plan with altered whitespace is not the saved snapshot."""
        source = {p: b"{}" for p in copy.PATHS}
        entries = {p: {} for p in copy.PATHS}
        entries.update({"README.md": {}, copy.MANIFEST: {}})
        plan = {
            "version": 1, "source_repository": copy.SOURCE,
            "destination_repository": copy.DESTINATION,
            "source_commit": collector.BASELINE_SOURCE,
            "destination_commit": "a" * 40,
            "readme_sha256": collector._hash(b"readme"),
            "files": [{"path": p, "size": 2, "sha256": collector._hash(b"{}")}
                      for p in copy.PATHS],
        }
        raw = copy.manifest(plan)
        changed = raw.replace(b',"plan_sha256"', b', "plan_sha256"')
        self.assertNotEqual(raw, changed)
        with patch.object(copy, "head", return_value=collector.BASELINE_SOURCE), \
             patch.object(collector, "_files", side_effect=[(source, {}), (source, entries)]), \
             patch.object(copy, "blob", side_effect=[changed]):
            result = collector.collect(Backend())
        self.assertEqual("UNKNOWN", result["classification"])

    def test_snapshot_commit_message_mismatch_unknown(self):
        """The copied commit message must bind to the approved plan hash."""
        source = {p: b"{}" for p in copy.PATHS}
        entries = {p: {} for p in copy.PATHS}
        entries.update({"README.md": {}, copy.MANIFEST: {}})
        plan = {
            "version": 1, "source_repository": copy.SOURCE,
            "destination_repository": copy.DESTINATION,
            "source_commit": collector.BASELINE_SOURCE,
            "destination_commit": "a" * 40,
            "readme_sha256": collector._hash(b"readme"),
            "files": [{"path": p, "size": 2, "sha256": collector._hash(b"{}")}
                      for p in copy.PATHS],
        }
        with patch.object(copy, "head", return_value=collector.BASELINE_SOURCE), \
             patch.object(collector, "_files", side_effect=[(source, {}), (source, entries)]), \
             patch.object(copy, "blob", return_value=copy.manifest(plan)), \
             patch.object(copy, "validate_commit", return_value={
                 "parents": [{"sha": "a" * 40}], "message": "incorrect message"}):
            result = collector.collect(Backend())
        self.assertEqual("UNKNOWN", result["classification"])

    def test_ancestry_same_without_network(self):
        backend = Backend()
        self.assertEqual("same", collector.ancestry(
            collector.ReadOnly(backend), "a" * 40, "a" * 40))
        self.assertEqual([], backend.calls)

    def test_ancestry_descendant(self):
        baseline, head = "a" * 40, "b" * 40
        backend = Backend()
        with patch.object(copy, "validate_commit", return_value={"parents": [{"sha": baseline}]}):
            self.assertEqual("descendant", collector.ancestry(
                collector.ReadOnly(backend), baseline, head))
        self.assertEqual(1, len(backend.calls))

    def test_ancestry_divergent_unknown(self):
        baseline, head = "a" * 40, "b" * 40
        with patch.object(copy, "validate_commit", return_value={"parents": []}):
            self.assertEqual("unknown", collector.ancestry(
                collector.ReadOnly(Backend()), baseline, head))

    def test_ancestry_depth_limit_unknown(self):
        baseline, head = "a" * 40, "b" * 40
        with patch.object(copy, "validate_commit", return_value={"parents": [{"sha": "c" * 40}]}):
            self.assertEqual("unknown", collector.ancestry(
                collector.ReadOnly(Backend()), baseline, head, max_commits=1))

    def test_ancestry_invalid_sha_no_network(self):
        backend = Backend()
        with self.assertRaises(copy.Stop):
            collector.ancestry(collector.ReadOnly(backend), "main", "a" * 40)
        self.assertEqual([], backend.calls)

    def test_ancestry_merge_parent_path(self):
        baseline, head = "a" * 40, "b" * 40
        other = "c" * 40
        def commit(value, expected=None):
            return {"parents": ([{"sha": other}, {"sha": baseline}]
                                if expected == head else [])}
        with patch.object(copy, "validate_commit", side_effect=commit):
            self.assertEqual("descendant", collector.ancestry(
                collector.ReadOnly(Backend()), baseline, head))

    def test_ancestry_cycle_unknown(self):
        baseline, head = "a" * 40, "b" * 40
        with patch.object(copy, "validate_commit", return_value={"parents": [{"sha": head}]}):
            self.assertEqual("unknown", collector.ancestry(
                collector.ReadOnly(Backend()), baseline, head))

    def test_ancestry_excessive_parents_unknown(self):
        baseline, head = "a" * 40, "b" * 40
        with patch.object(copy, "validate_commit", return_value={
                "parents": [{"sha": "c" * 40}] * 17}):
            self.assertEqual("unknown", collector.ancestry(
                collector.ReadOnly(Backend()), baseline, head))

    def test_ancestry_invalid_limit_rejected(self):
        with self.assertRaises(copy.Stop):
            collector.ancestry(collector.ReadOnly(Backend()),
                               "a" * 40, "b" * 40, max_commits=0)

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
