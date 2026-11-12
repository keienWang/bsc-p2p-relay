# SPDX-License-Identifier: GPL-3.0-or-later
"""Python-only contracts. The fake runner never invokes Go or any network."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import bls_stage0_inventory as stage


class FakeRun:
    def __init__(self, root, change=None):
        self.root, self.change, self.commands = root, change, []
        self.state = None

    def __call__(self, command, cwd, env, deadline):
        self.commands.append(command[1:])
        self.state = cwd.parent
        if command[1] == "env":
            goroot = self.root / "toolchain"
            (goroot / "src/unsafe").mkdir(parents=True, exist_ok=True)
            (goroot / "src/unsafe/unsafe.go").write_text("DO_NOT_EXPORT_STD_SOURCE")
            (goroot / "LICENSE").write_text("DO_NOT_EXPORT_STD_LICENSE")
            (goroot / "VERSION").write_text(stage.GO_VERSION + "\n")
            info = {k: env.get(k, "") for k in stage.ENV_KEYS}
            info.update(GOVERSION=stage.GO_VERSION, GOHOSTOS="linux", GOHOSTARCH="amd64",
                        GOMOD=str(cwd / "go.mod"), GOROOT=str(goroot), GOWORK="off")
            if self.change == "tool":
                info["GOVERSION"] = "go1.23.0"
            if self.change == "goroot":
                info["GOROOT"] = str(self.root)
            if self.change == "workspace":
                info["GOWORK"] = "/private/secret/go.work"
            if self.change == "env_extra_object":
                return json.dumps(info) + "\n{}"
            if self.change == "env_missing_key":
                del info["GOARCH"]
            if self.change == "env_unknown_key":
                info["UNKNOWN"] = "/private/should-not-print"
            if self.change == "goamd64":
                info["GOAMD64"] = "v2"
            return json.dumps(info)
        objects = []
        for name, (version, checksum, modsum) in stage.PINS.items():
            directory = Path(env["GOMODCACHE"]) / (name + "@" + version)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "LICENSE").write_text("DO_NOT_EXPORT_MODULE_LICENSE")
            objects.append({"Path": name, "Version": version, "Sum": checksum,
                            "GoModSum": modsum, "Dir": str(directory)})
            if name == stage.BLS:
                for relative, text in (("crypto/bls/blst/init.go", "mock-init"),
                                       ("crypto/bls/blst/public_key.go", "mock-public"),
                                       ("cache/nonblocking/lru.go", "mock-cache")):
                    p = directory / relative
                    p.parent.mkdir(parents=True, exist_ok=True)
                    if not p.exists():
                        p.write_text(text)
            if name == stage.BLST:
                for relative in ("bindings/go/blst.go", "bindings/blst.h", "src/server.c", "build/assembly.S"):
                    p = directory / relative
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text("DO_NOT_EXPORT_NATIVE_SOURCE")
        if command[1:3] == ["mod", "download"]:
            if self.change == "download":
                objects[0]["Sum"] = "h1:WRONG"
            return "\n".join(json.dumps(x) for x in objects)
        if command[1:4] == ["list", "-m", "-json"]:
            if self.change == "sum":
                objects[0]["Sum"] = "h1:WRONG"
            if self.change == "replace":
                objects[0]["Replace"] = {"Path": "/private/secret"}
            return "\n".join(json.dumps(x) for x in objects)
        if self.change == "temporary_lock":
            (cwd / "go.sum").write_text("changed")
        if self.change == "tool_binary":
            (self.root / "toolchain/bin/go").write_text("changed")
        if self.change == "source_input":
            (self.root / "go.mod").write_text("changed")
        roots = {o["Path"]: o["Dir"] for o in objects}
        module = {o["Path"]: {"Path": o["Path"], "Version": o["Version"]} for o in objects}
        pdir = str(cwd / "internal/blsgraphprobe")
        generated = Path(env["GOCACHE"]) / "_testmain.go"
        generated.write_text("DO_NOT_EXPORT_GENERATED_SOURCE")
        packages = [
            {"ImportPath": "unsafe", "Standard": True, "Dir": str(self.root / "toolchain/src/unsafe"),
             "GoFiles": ["unsafe.go"], "Imports": []},
            {"ImportPath": stage.BLS + "/cache/nonblocking", "Dir": roots[stage.BLS] + "/cache/nonblocking",
             "Module": module[stage.BLS], "GoFiles": ["lru.go"], "Imports": []},
            {"ImportPath": stage.BLST + "/bindings/go", "Dir": roots[stage.BLST] + "/bindings/go",
             "Module": module[stage.BLST], "CgoFiles": ["blst.go"], "Imports": ["C"]},
            {"ImportPath": stage.BLS + "/crypto/bls/blst", "Dir": roots[stage.BLS] + "/crypto/bls/blst",
             "Module": module[stage.BLS], "GoFiles": ["init.go", "public_key.go"],
             "Imports": [stage.BLS + "/cache/nonblocking", stage.BLST + "/bindings/go"]},
            {"ImportPath": stage.PROBE, "Dir": pdir, "Module": {"Main": True, "Path": stage.MAIN},
             "TestGoFiles": ["graph_test.go"], "TestImports": [stage.BLS + "/crypto/bls/blst"],
             "Imports": []},
            {"ImportPath": stage.PROBE + " [" + stage.PROBE + ".test]", "ForTest": stage.PROBE,
             "Dir": pdir, "Module": {"Main": True, "Path": stage.MAIN},
             "GoFiles": ["graph_test.go"], "Imports": [stage.BLS + "/crypto/bls/blst"]},
            {"ImportPath": stage.PROBE + ".test", "Name": "main", "GoFiles": [str(generated)],
             "Imports": [stage.PROBE, "unsafe"], "ImportMap": {stage.PROBE: stage.PROBE + " [" + stage.PROBE + ".test]"}},
        ]
        if self.change == "upstream_test":
            packages[3]["ForTest"] = stage.BLS + "/crypto/bls/blst"
        if self.change == "duplicate":
            packages.append(dict(packages[4]))
        if self.change == "missing_variant":
            del packages[5]
        if self.change == "package_error":
            packages[3]["Error"] = {"Err": "/private/secret: source text"}
        if self.change == "unresolved":
            packages[3]["Imports"].append("unknown/missing")
        if self.change == "source":
            Path(roots[stage.BLS] + "/crypto/bls/blst/init.go").write_text("changed")
        if self.change == "generated_escape":
            packages[-1]["GoFiles"] = [str(self.root / "_testmain.go")]
            (self.root / "_testmain.go").write_text("private")
        if self.change == "generated_symlink":
            linked = Path(env["GOCACHE"]) / "linked-source"
            linked.write_text("private")
            generated.unlink()
            generated.symlink_to(linked)
        if self.change == "generated_missing":
            packages[-1]["GoFiles"] = ["_testmain.go"]
        if self.change == "path":
            packages[3]["GoFiles"] = ["../../../../private"]
        return "\n".join(json.dumps(x) for x in packages)


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.files = {"LICENSE": "DO_NOT_EXPORT_MAIN_LICENSE",
                      "go.mod": "module " + stage.MAIN + "\n",
                      "go.sum": "".join(path + " " + version + " " + value + "\n" +
                                       path + " " + version + "/go.mod " + modsum + "\n"
                                       for path, (version, value, modsum) in stage.PINS.items()),
                      "compat/stunv2/go.mod": "module test/stun\n"}
        for relative, text in self.files.items():
            p = self.root / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        p = self.root / stage.PROBE_FILE
        p.parent.mkdir(parents=True)
        p.write_text("mock probe")
        self.go = self.root / "toolchain/bin/go"
        self.go.parent.mkdir(parents=True)
        self.go.write_text("mock executable; never run")
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(stage, "INPUTS", {p: stage.sha(t.encode()) for p, t in self.files.items()}).start()
        mock.patch.object(stage, "PROBE_SHA", stage.sha(b"mock probe")).start()
        mock.patch.object(stage, "PRYSM_FILES", {"crypto/bls/blst/init.go": stage.sha(b"mock-init"),
                                               "crypto/bls/blst/public_key.go": stage.sha(b"mock-public")}).start()

    def collect(self, change=None):
        runner = FakeRun(self.root, change)
        result = stage.collect(self.root, self.go, runner)
        return json.loads(result), runner

    def test_full_mock_preserves_instances_and_safe_data(self):
        report, runner = self.collect()
        self.assertFalse(runner.state.exists(), "temporary caches must be deleted")
        text = json.dumps(report)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("DO_NOT_EXPORT", text)
        ids = [p["id"] for p in report["packages"]]
        self.assertIn(stage.PROBE, ids)
        self.assertIn(stage.PROBE + " [" + stage.PROBE + ".test]", ids)
        testmain = next(p for p in report["packages"] if p["id"] == stage.PROBE + ".test")
        self.assertEqual(testmain["import_map"][stage.PROBE], stage.PROBE + " [" + stage.PROBE + ".test]")
        self.assertTrue(testmain["generated_sources"][0]["materialized"])
        self.assertEqual(len(testmain["generated_sources"][0]["sha256"]), 64)
        self.assertFalse(report["tests_executed"])
        self.assertFalse(report["native_include_closure_proven"])
        self.assertFalse(report["license_approval"])
        self.assertEqual([item["path"] for item in report["found_license_files"]["main"]], ["LICENSE"])
        self.assertEqual(report["baseline_public_commit"], "560ddfc85e05f9a22d16d3b420396d10605e00c8")
        self.assertFalse(report["modules"][stage.LRU]["has_selected_packages"])
        self.assertEqual([c[0] for c in runner.commands], ["env", "mod", "list", "list"])
        self.assertEqual(runner.commands[2], ["list", "-deps", "-test", "-json", "./internal/blsgraphprobe"])

    def test_expected_missing_testmain_source_is_explicit(self):
        report, _ = self.collect("generated_missing")
        generated = next(p for p in report["packages"] if p["id"] == stage.PROBE + ".test")["generated_sources"][0]
        self.assertFalse(generated["materialized"])
        self.assertIsNone(generated["sha256"])

    def test_fail_closed_mutations(self):
        cases = {
            "tool": "go_environment_mismatch", "workspace": "go_environment_mismatch",
            "goroot": "go_root_mismatch", "generated_symlink": "unsafe_generated_source",
            "download": "download_checksum_mismatch", "sum": "module_sum_mismatch",
            "replace": "unexpected_module_replacement", "upstream_test": "upstream_tests_selected",
            "duplicate": "duplicate_package_instance", "missing_variant": "missing_test_graph_instances",
            "package_error": "package_error", "unresolved": "unresolved_import_instance",
            "source": "upstream_source_mismatch", "generated_escape": "generated_source_escape",
            "path": "unsafe_relative_name", "temporary_lock": "temporary_lock_or_compat_changed",
        }
        for change, expected in cases.items():
            with self.subTest(change=change):
                runner = FakeRun(self.root, change)
                with self.assertRaisesRegex(stage.GateError, "^" + expected + "$"):
                    stage.collect(self.root, self.go, runner)
                self.assertFalse(runner.state.exists())

    def test_go_environment_has_one_complete_object(self):
        for change in ("env_extra_object", "env_missing_key", "env_unknown_key"):
            with self.subTest(change=change), self.assertRaisesRegex(stage.GateError, "go_environment_shape"):
                self.collect(change)

    def test_amd64_target_is_rejected_and_verified(self):
        with self.assertRaisesRegex(stage.GateError, "external_target"):
            stage.reject_environment({"GOAMD64": "v2"})
        stage.reject_environment({"GOAMD64": "v1"})
        with self.assertRaisesRegex(stage.GateError, "go_environment_mismatch"):
            self.collect("goamd64")
        report, _ = self.collect()
        self.assertEqual(report["tool"]["goamd64"], "v1")

    def test_tool_binary_change_is_not_reported_as_original_tool(self):
        with self.assertRaisesRegex(stage.GateError, "go_binary_changed"):
            self.collect("tool_binary")

    def test_replacement_failure_has_only_safe_coordinates(self):
        version, checksum, modsum = stage.PINS[stage.BLS]
        original = {"Path": stage.BLS, "Version": version, "Sum": checksum,
                    "GoModSum": modsum, "Dir": "/secret/not-exported"}
        for replacement, expected in (
            ({"Path": "github.com/bnb-chain/bsc", "Version": "v1.8.2"},
             {"kind": "versioned_module", "path": "github.com/bnb-chain/bsc", "version": "v1.8.2"}),
            ({"Path": "/secret/not-exported"}, {"kind": "local_path", "path_redacted": True}),
        ):
            obj = dict(original, Replace=replacement)
            with self.assertRaisesRegex(stage.GateError, "unexpected_module_replacement") as caught:
                stage.module_record(obj, {}, self.root)
            data = caught.exception.safe_diagnostics
            self.assertEqual(data["effective_module"], expected)
            self.assertNotIn("/secret", json.dumps(data))
        with self.assertRaises(stage.GateError):
            stage.module_record(dict(original, Replace={"Path": "https://host/?secret=x", "Version": "v1.2.3"}), {}, self.root)

    def test_source_byte_budget_and_file_cap(self):
        p = self.root / "small"
        p.write_bytes(b"123456789")
        with mock.patch.object(stage, "MAX_SOURCE_BYTES", 8):
            with self.assertRaisesRegex(stage.GateError, "source_byte_limit"):
                stage.file_record(self.root, p, {"bytes": 0})
        with mock.patch.object(stage, "MAX_FILE_BYTES", 8):
            with self.assertRaisesRegex(stage.GateError, "oversized_file"):
                stage.read_file(self.root, "small")

    def test_original_input_mutation_detected(self):
        with self.assertRaisesRegex(stage.GateError, "input_hash_mismatch"):
            self.collect("source_input")

    def test_unknown_compat_file_rejected(self):
        (self.root / "compat/stunv2/extra.go").write_text("unknown")
        with self.assertRaisesRegex(stage.GateError, "unknown_compat_input"):
            stage.input_hashes(self.root)

    def test_workspace_file_rejected(self):
        (self.root / "go.work").write_text("go 1.26.11")
        with self.assertRaisesRegex(stage.GateError, "workspace_file"):
            stage.input_hashes(self.root)

    def test_symlink_file_rejected(self):
        (self.root / "secret").write_text("secret")
        (self.root / "link").symlink_to(self.root / "secret")
        with self.assertRaisesRegex(stage.GateError, "missing_or_symlink_file"):
            stage.read_file(self.root, "link")

    def test_external_configuration_rejected(self):
        for env in ({"GOFLAGS": "-modfile=evil.mod"}, {"GOFLAGS": "-overlay=evil.json"},
                    {"GOWORK": "auto"}, {"GOWORK": "/secret/go.work"},
                    {"GOTOOLCHAIN": "auto"}, {"GOPROXY": "https://secret"},
                    {"CGO_CFLAGS": "-include evil.h"}, {"GOOS": "darwin"}):
            with self.subTest(env=env), self.assertRaises(stage.GateError):
                stage.reject_environment(env)
        stage.reject_environment({})
        stage.reject_environment({"GOTOOLCHAIN": "local", "GOWORK": "off"})

    def test_relative_names_rejected(self):
        for name in ("/secret", "../secret", "a/../b", "a\\b", "a//b", "a?secret", ""):
            with self.subTest(name=name), self.assertRaises(stage.GateError):
                stage.relative(name)

    def test_import_id_rules(self):
        good = stage.PROBE + " [" + stage.PROBE + ".test]"
        self.assertEqual(stage.identifier(good), good)
        for name in ("/secret", "a/../b", "bad [other.test]", "x?token=secret"):
            with self.subTest(name=name), self.assertRaises(stage.GateError):
                stage.identifier(name)

    def test_json_and_sums_are_strict(self):
        self.assertEqual(len(stage.stream_json('{}\n{}')), 2)
        for text in ("", "[]", '{"a":', "null"):
            with self.subTest(text=text), self.assertRaises(stage.GateError):
                stage.stream_json(text)
        with self.assertRaisesRegex(stage.GateError, "duplicate_go_sum"):
            stage.sum_entries("a v1 h1:x\na v1 h1:x\n")

    def test_errors_do_not_print_sensitive_detail(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(stage.sys, "argv", ["collector"]), mock.patch.object(stage, "reject_environment"), \
             mock.patch.object(stage.shutil, "which", return_value="/mock/go"), \
             mock.patch.object(stage, "collect", side_effect=ValueError("/private/secret source")), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(stage.main(), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "BLS_STAGE0_ERROR:metadata_collection_error\n")

    def test_unknown_cli_rejected_without_go(self):
        with mock.patch.object(stage.sys, "argv", ["collector", "--modfile=evil"]), \
             mock.patch.object(stage.shutil, "which") as which, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(stage.main(), 1)
        which.assert_not_called()

    def test_clean_environment_has_no_auth_or_database_route(self):
        env = stage.clean_env(self.go, self.root)
        self.assertEqual(env["GOPROXY"], "https://proxy.golang.org")
        self.assertEqual(env["GOSUMDB"], "off")
        self.assertEqual(env["GOAUTH"], "off")
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GOFLAGS"], stage.FLAGS)
        self.assertNotIn("GOVULNDB", env)
        self.assertNotIn("GITHUB_TOKEN", env)


class ResourceContracts(unittest.TestCase):
    def test_actual_frozen_repository_inputs(self):
        root = Path(__file__).resolve().parents[1]
        values = stage.input_hashes(root)
        self.assertEqual(values["go.mod"], stage.INPUTS["go.mod"])
        self.assertEqual(values[stage.PROBE_FILE], stage.PROBE_SHA)

    def test_expired_deadline_does_not_start_child(self):
        with mock.patch.object(stage.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(stage.GateError, "^deadline$"):
                stage.capture(["never"], Path("."), {}, 0)
        popen.assert_not_called()

    def test_output_cap_cleans_process_group_without_printing(self):
        command = [stage.sys.executable, "-c",
                   "import time; print('private' * 100, flush=True); time.sleep(30)"]
        with mock.patch.object(stage, "MAX_RAW", 32), \
             mock.patch.object(stage.os, "killpg", wraps=os.killpg) as kill:
            with self.assertRaisesRegex(stage.GateError, "^tool_output_limit$") as caught:
                stage.capture(command, Path("."), {}, stage.time.monotonic() + 5)
        signals = [call.args[1] for call in kill.call_args_list]
        self.assertIn(stage.signal.SIGTERM, signals)
        self.assertIn(stage.signal.SIGKILL, signals)
        self.assertGreater(caught.exception.safe_diagnostics["tool_stdout_bytes"], 32)
        self.assertNotIn("private", json.dumps(caught.exception.safe_diagnostics))

    def test_per_command_cap_cleans_process_group(self):
        command = [stage.sys.executable, "-c", "import time; time.sleep(30)"]
        with mock.patch.object(stage, "COMMAND_SECONDS", 0.1), \
             mock.patch.object(stage.os, "killpg", wraps=os.killpg) as kill:
            with self.assertRaisesRegex(stage.GateError, "^deadline$"):
                stage.capture(command, Path("."), {}, stage.time.monotonic() + 10)
        self.assertIn(stage.signal.SIGTERM, [call.args[1] for call in kill.call_args_list])
        self.assertIn(stage.signal.SIGKILL, [call.args[1] for call in kill.call_args_list])


    def test_cleanup_waits_are_bounded_when_reap_fails(self):
        proc = mock.Mock(pid=12345)
        proc.wait.side_effect = stage.subprocess.TimeoutExpired("mock", 1)
        proc.poll.return_value = None
        with mock.patch.object(stage.os, "killpg") as kill:
            self.assertFalse(stage.stop_group(proc))
        self.assertEqual([c.kwargs for c in proc.wait.call_args_list],
                         [{"timeout": 0.5}, {"timeout": 1.0}])
        self.assertEqual([c.args[1] for c in kill.call_args_list],
                         [stage.signal.SIGTERM, stage.signal.SIGKILL])

    def test_output_cap_terminates_a_real_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "owned-child.pid"
            script = ("import pathlib,subprocess,sys,time; "
                      "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                      "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
                      "print('x'*128,flush=True); time.sleep(30)")
            with mock.patch.object(stage, "MAX_RAW", 32):
                with self.assertRaisesRegex(stage.GateError, "^tool_output_limit$"):
                    stage.capture([stage.sys.executable, "-c", script, str(pidfile)],
                                  Path("."), {}, stage.time.monotonic() + 5)
            pid = int(pidfile.read_text())
            def live():
                try:
                    stat = Path("/proc") / str(pid) / "stat"
                    return stat.read_text().rpartition(") ")[2].split()[0] != "Z"
                except FileNotFoundError:
                    return False
            try:
                deadline = stage.time.monotonic() + 1
                while live() and stage.time.monotonic() < deadline:
                    stage.time.sleep(0.01)
                self.assertFalse(live(), "descendant survived process-group cleanup")
            finally:
                if live():
                    os.kill(pid, stage.signal.SIGKILL)



if __name__ == "__main__":
    unittest.main()
