# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic private-tree contracts only. No upstream code or Go is executed."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import bls_cache_race_ci as runner
import bls_stage0_inventory as base


class PrivateTreeContracts(unittest.TestCase):
    NAMES = ("bindings/c#/poc.cs", "bindings/c#/poc.csproj", "bindings/c#/run.me",
             "bindings/c#/supranational.blst.cs")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.source = self.state / "source"
        self.source.mkdir()
        self.deadline = time.monotonic() + 10
        self.data = {}
        for index, name in enumerate(self.NAMES):
            data = ("SYNTHETIC_NOT_CSHARP_SOURCE_" + str(index)).encode()
            self.data[name] = data
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def test_all_four_names_are_hashed_and_copied_without_public_relaxation(self):
        original = runner.tree_manifest(self.source, self.deadline)
        expected = {name: base.sha(data) for name, data in self.data.items()}
        self.assertEqual(original, {"files": expected, "bytes": sum(map(len, self.data.values())),
                                   "file_count": 4, "manifest_sha256": base.sha(runner.canonical(expected))})
        target = self.state / "copy"
        runner.copy_tree(self.source, target, original, self.deadline)
        self.assertEqual(runner.tree_manifest(target, self.deadline), original)
        for name, data in self.data.items():
            self.assertEqual((target / name).read_bytes(), data)
            with self.assertRaisesRegex(base.GateError, "unsafe_relative_name"):
                base.relative(name)
            with self.assertRaisesRegex(base.GateError, "unsafe_relative_name"):
                base.file_record(self.source, self.source / name)
        safe = runner.safe_tree(original)
        self.assertNotIn("bindings", json.dumps(safe))
        self.assertNotIn("SYNTHETIC", json.dumps(safe))
        self.assertEqual(safe["file_count"], 4)

    def test_private_path_rejects_traversal_absolute_control_and_unknown_characters(self):
        bad = [None, 7, "", "/absolute", "//absolute", ".", "..", "../escape", "a/../b", "a/./b",
               "a//b", "a/", "a\\b", "a\x00b", "a\nb", "a\rb", "a\tb", "a\x7fb", "a b",
               "a%b", "a:b", "a?b", "a[b]", "a(b)", "aé", "a\udcff"]
        for name in bad:
            with self.subTest(name=repr(name)), self.assertRaises(base.GateError):
                runner.private_tree_path(name)
        self.assertEqual(str(runner.private_tree_path("a#b/c.go")), "a#b/c.go")

    def test_private_read_rejects_symlink_files_directories_and_roots(self):
        outside = self.state / "outside"
        outside.write_text("PRIVATE_SENTINEL")
        linked = self.source / "link#"
        for target in (outside, self.source / self.NAMES[0], self.source / "bindings"):
            with self.subTest(target=target.name):
                linked.symlink_to(target, target_is_directory=target.is_dir())
                with self.assertRaises(base.GateError):
                    runner.read_tree_file(self.source, "link#" + ("/c#/poc.cs" if target.is_dir() else ""))
                with self.assertRaises(base.GateError):
                    runner.tree_manifest(self.source, self.deadline)
                linked.unlink()
        rootlink = self.state / "rootlink"
        rootlink.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(base.GateError):
            runner.read_tree_file(rootlink, self.NAMES[0])
        with self.assertRaises(base.GateError):
            runner.read_tree_file(self.source, "../outside")
        with self.assertRaises(base.GateError):
            runner.read_tree_file(self.source, str(outside))

    def test_unknown_filename_is_not_silently_skipped(self):
        (self.source / "unknown%name").write_text("synthetic")
        with self.assertRaisesRegex(base.GateError, "unsafe_private_tree_name"):
            runner.tree_manifest(self.source, self.deadline)

    def test_private_read_retains_missing_and_file_size_guards(self):
        with self.assertRaisesRegex(base.GateError, "missing_private_tree_file"):
            runner.read_tree_file(self.source, "missing#")
        with mock.patch.object(base, "MAX_FILE_BYTES", 1), self.assertRaisesRegex(base.GateError, "oversized_file"):
            runner.read_tree_file(self.source, self.NAMES[0])

    def test_manifest_tampering_fails_instead_of_skipping_or_rewriting_inputs(self):
        original = runner.tree_manifest(self.source, self.deadline)
        variants = []
        for key, value in (("bytes", 0), ("file_count", 0), ("manifest_sha256", "0" * 64)):
            variants.append(dict(original, **{key: value}))
        variants.append(dict(original, files=dict(original["files"], **{self.NAMES[0]: "0" * 64})))
        variants.append(dict(original, files={"../outside": "0" * 64}))
        variants.append(dict(original, files={"/absolute": "0" * 64}))
        for index, bad in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(base.GateError):
                runner.copy_tree(self.source, self.state / ("copy" + str(index)), bad, self.deadline)
        self.assertEqual(runner.tree_manifest(self.source, self.deadline), original)
        self.assertFalse((self.state / "outside").exists())

    def test_selected_and_license_rules_remain_strict(self):
        (self.source / "LICENSE_#").write_text("SYNTHETIC_LICENSE")
        with self.assertRaisesRegex(base.GateError, "unsafe_relative_name"):
            base.found_licenses(self.source, {self.source})
        with self.assertRaisesRegex(base.GateError, "unsafe_relative_name"):
            base.read_file(self.source, self.NAMES[0])

    def test_validation_phase_projection_is_enum_only(self):
        for phase in (None, *sorted(runner.VALIDATION_PHASES)):
            runner.checkpoint(self.state, current_validation=phase)
            progress = json.loads((self.state / "progress.json").read_text())
            self.assertEqual(runner.export_progress(progress, None)["current_validation"], phase)
        for value in ("SENTINEL_PRIVATE_PATH", {"raw": "SENTINEL"}, ["selected_files"], 1, False):
            progress["current_validation"] = value
            with self.subTest(value=value), self.assertRaises(base.GateError):
                runner.export_progress(progress, None)
        progress["current_validation"] = "selected_files"
        progress["private_filename"] = "SENTINEL"
        with self.assertRaises(base.GateError):
            runner.export_progress(progress, None)

    def test_new_command_clears_stale_validation_phase(self):
        runner.checkpoint(self.state, current_validation="whole_module_manifests")
        (self.state / "logs").mkdir()
        def merged(*args):
            (self.state / "logs" / "baseline-compile.log").write_bytes(b"")
            return 0, ""
        with mock.patch.object(runner.process, "capture", side_effect=merged):
            runner.command(self.state, "baseline-compile", ["NEVER_EXECUTED"], {}, runner.process.Budget(1))
        progress = json.loads((self.state / "progress.json").read_text())
        self.assertIsNone(runner.export_progress(progress, None)["current_validation"])


if __name__ == "__main__":
    unittest.main()
