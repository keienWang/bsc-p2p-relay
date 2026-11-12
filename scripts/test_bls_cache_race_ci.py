# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage1 Python/mock contracts only. Never invoke Go, patch or namespaces."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import bls_cache_race_ci as runner
import bls_stage0_inventory as base
from test_bls_cache_race_evidence import stream, race_block, ROOT as RACE_ROOT


def helper_objects(go):
    objects = []
    for name, filename, imports in (("internal/test2json", "json.go", []),
                                     (runner.HELPER_PACKAGE, "main.go", ["internal/test2json"])):
        directory = go.parent.parent / "src" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text("DO_NOT_EXPORT_HELPER_SOURCE")
        objects.append({"ImportPath": name, "Goroot": True, "Name": "main" if name == runner.HELPER_PACKAGE else "test2json",
                        "Dir": str(directory), "GoFiles": [filename], "Imports": imports})
    return objects


class GraphFake:
    def __init__(self, state, go, mutate=None):
        self.state, self.go, self.mutate = state, go, mutate
        self.calls = []

    def __call__(self, state, label, args, env, budget, timeout=90, allow_failure=False, cwd=None):
        self.calls.append((label, args))
        side = "baseline" if "baseline" in label else "candidate"
        tree = state / (side + "-prysm")
        original = {"Path": base.BLS, "Version": base.PINS[base.BLS][0], "Dir": str(tree),
                    "Replace": {"Path": "../" + tree.name, "Dir": str(tree)}}
        selected = {base.BLS: original}
        for name in (base.BLST, base.LRU):
            version, checksum, modsum = base.PINS[name]
            root = state / "modcache" / (name + "@" + version)
            root.mkdir(parents=True, exist_ok=True)
            (root / "LICENSE").write_text("DO_NOT_EXPORT_LICENSE")
            if name == base.BLST:
                (root / "bindings/go").mkdir(parents=True, exist_ok=True)
                (root / "bindings/go/blst.go").write_text("mock native")
            else:
                (root / "lru.go").write_text("mock lru")
            if name == base.BLST or side == "candidate":
                selected[name] = {"Path": name, "Version": version, "Sum": checksum,
                                  "GoModSum": modsum, "Dir": str(root)}
        if "-m" in args:
            return "\n".join(json.dumps(x) for x in selected.values()), {"exit_code": 0}
        std = self.go.parent.parent / "src/unsafe"
        std.mkdir(parents=True, exist_ok=True)
        (std / "unsafe.go").write_text("mock standard")
        cache_import = base.BLS + "/cache/nonblocking" if side == "baseline" else base.LRU
        packages = [
            {"ImportPath": "unsafe", "Standard": True, "Dir": str(std), "GoFiles": ["unsafe.go"]},
            {"ImportPath": base.BLST + "/bindings/go", "Module": selected[base.BLST],
             "Dir": selected[base.BLST]["Dir"] + "/bindings/go", "CgoFiles": ["blst.go"], "Imports": ["C"]},
            {"ImportPath": base.BLS + "/crypto/bls/blst", "Module": original,
             "Dir": str(tree / "crypto/bls/blst"), "GoFiles": ["init.go", "public_key.go"],
             "Imports": [cache_import, base.BLST + "/bindings/go"]},
            {"ImportPath": runner.events.PACKAGE, "Module": original, "Dir": str(tree / "cachepatchtests"),
             "TestGoFiles": [Path(runner.FIXTURE).name], "TestImports": [base.BLS + "/crypto/bls/blst"]},
            {"ImportPath": runner.events.PACKAGE + " [" + runner.events.PACKAGE + ".test]", "Module": original,
             "Dir": str(tree / "cachepatchtests"), "GoFiles": [Path(runner.FIXTURE).name],
             "ForTest": runner.events.PACKAGE, "Imports": [base.BLS + "/crypto/bls/blst"]},
            {"ImportPath": runner.events.PACKAGE + ".test", "Name": "main", "GoFiles": ["_testmain.go"],
             "Imports": [runner.events.PACKAGE, "unsafe"],
             "ImportMap": {runner.events.PACKAGE: runner.events.PACKAGE + " [" + runner.events.PACKAGE + ".test]"}},
        ]
        if side == "baseline":
            packages.append({"ImportPath": cache_import, "Module": original, "Dir": str(tree / "cache/nonblocking"), "GoFiles": ["lru.go"]})
        else:
            packages.append({"ImportPath": cache_import, "Module": selected[base.LRU], "Dir": selected[base.LRU]["Dir"], "GoFiles": ["lru.go"]})
        if self.mutate:
            self.mutate(packages)
        return "\n".join(json.dumps(x) for x in packages), {"exit_code": 0}


class RunnerContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.repo = Path(__file__).resolve().parents[1]
        self.work = self.state / "work"
        self.work.mkdir()
        self.go = self.state / "toolchain/bin/go"
        self.go.parent.mkdir(parents=True)
        self.go.write_text("mock executable never invoked")
        (self.go.parent.parent / "LICENSE").write_text("DO_NOT_EXPORT_STD_LICENSE")
        (self.work / "go.mod").write_bytes((self.repo / "go.mod").read_bytes())
        (self.work / "go.sum").write_bytes((self.repo / "go.sum").read_bytes())
        for side in runner.SIDES:
            tree = self.state / (side + "-prysm")
            for name in ("LICENSE.md", "go.mod", "crypto/bls/blst/init.go", "crypto/bls/blst/public_key.go",
                         "cache/nonblocking/lru.go", runner.FIXTURE):
                path = tree / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("DO_NOT_EXPORT_SOURCE")
        self.budget = runner.process.Budget(20)

    def test_actual_six_materials_are_exact_and_no_full_source(self):
        material = runner.verify_material(self.repo)
        self.assertEqual(len(material), 6)
        self.assertEqual(material[runner.PATCH], runner.PATCH_SHA)
        self.assertEqual(material["blackbox_test.go.txt"], runner.FIXTURE_SHA)
        self.assertNotIn("reference", material)

    def test_private_modfile_preserves_root_and_only_adds_one_local_replace(self):
        original = (self.work / "go.mod").read_bytes()
        sums = (self.work / "go.sum").read_bytes()
        result = runner.make_modfile(self.work, "baseline")
        self.assertEqual((self.work / "go.mod").read_bytes(), original)
        self.assertEqual((self.work / "baseline.mod").read_bytes(), original +
                         ("\nreplace " + base.BLS + " => ../baseline-prysm\n").encode())
        self.assertEqual((self.work / "baseline.sum").read_bytes(), sums)
        self.assertEqual(set(result), {"baseline.mod", "baseline.sum"})
        with self.assertRaises(base.GateError):
            runner.make_modfile(self.work, "../escape")

    def test_prepare_mock_uses_official_origin_then_only_private_derived_copies(self):
        state = self.state / "fresh"
        state.mkdir()
        version_file = self.go.parent.parent / "VERSION"
        version_file.write_text(base.GO_VERSION + "\n")
        for name in ("asm", "compile", "link", "cgo"):
            path = self.go.parent.parent / "pkg/tool/linux_amd64" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("mock tool never run")
        tools = self.state / "platform-tools"
        tools.mkdir()
        for name in ("git", "gcc", "as", "ld", "sudo", "timeout", "unshare", "setpriv", "ip", "prlimit"):
            (tools / name).write_text("mock platform tool never run")
        calls = []
        graph = GraphFake(state, self.go)
        original_data = {"crypto/bls/blst/init.go": b"mock original init",
                         "crypto/bls/blst/public_key.go": b"mock original public"}
        def fake(state, label, args, env, budget, timeout=90, allow_failure=False, cwd=None):
            calls.append(label)
            if label == "prepare-test2json-graph":
                self.assertEqual(env["GOPROXY"], "off")
                self.assertEqual(env["CC"], str(tools / "gcc"))
                return "\n".join(json.dumps(v) for v in helper_objects(self.go)), {"exit_code": 0}
            self.assertEqual(env["GOPROXY"], "https://proxy.golang.org")
            if label == "prepare-go-env":
                value = {k: env.get(k, "") for k in base.ENV_KEYS}
                value.update(GOVERSION=base.GO_VERSION, GOHOSTOS="linux", GOHOSTARCH="amd64",
                             GOROOT=str(self.go.parent.parent), GOMOD=str(state / "work/go.mod"))
                return json.dumps(value), {"exit_code": 0}
            if label == "prepare-pins":
                values = []
                for name, (version, checksum, modsum) in base.PINS.items():
                    root = state / "modcache" / (name + "@" + version)
                    root.mkdir(parents=True)
                    (root / "LICENSE").write_text("mock license")
                    (root / "go.mod").write_text("module " + name + "\n")
                    if name == base.BLS:
                        for rel, data in dict(original_data, **{"cache/nonblocking/lru.go": b"mock cache"}).items():
                            path = root / rel
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(data)
                    values.append({"Path": name, "Version": version, "Sum": checksum, "GoModSum": modsum, "Dir": str(root)})
                return "\n".join(json.dumps(v) for v in values), {"exit_code": 0}
            if label in ("prepare-verify-origin", "prepare-verify-modules"):
                return "all modules verified\n", {"exit_code": 0}
            if label == "patch-check":
                return "", {"exit_code": 0}
            if label == "patch-apply":
                for rel, data in original_data.items():
                    (cwd / rel).write_bytes(data + b" patched")
                return "", {"exit_code": 0}
            return graph(state, label, args, env, budget, timeout, allow_failure, cwd)
        with mock.patch.object(runner, "command", fake), \
             mock.patch.object(base, "PRYSM_FILES", {n: base.sha(d) for n, d in original_data.items()}), \
             mock.patch.object(os, "geteuid", return_value=1001), mock.patch.object(os, "getuid", return_value=1001), \
             mock.patch.object(os, "getgid", return_value=1001), mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(runner.shutil, "which", side_effect=lambda name: str(tools / name)):
            receipt = runner.prepare(self.repo, state, self.go)
        self.assertIn("prepare-test2json-graph", calls)
        self.assertNotIn("test2json-compile", calls)
        self.assertNotIn("go-test2json", receipt["tool_paths"])
        self.assertIn("go-asm", receipt["tool_paths"])
        self.assertEqual(receipt["helper_graph"]["package"], runner.HELPER_PACKAGE)
        self.assertIn("prepare-verify-origin", calls)
        self.assertLess(calls.index("prepare-verify-origin"), calls.index("patch-apply"))
        self.assertEqual(receipt["identity"]["copied_inputs"], base.INPUTS)
        for rel, data in original_data.items():
            self.assertEqual((Path(receipt["donor"]) / rel).read_bytes(), data)
            self.assertEqual((state / "baseline-prysm" / rel).read_bytes(), data)
            self.assertEqual((state / "candidate-prysm" / rel).read_bytes(), data + b" patched")
        for side in runner.SIDES:
            self.assertEqual(base.sha((state / (side + "-prysm") / runner.FIXTURE).read_bytes()), runner.FIXTURE_SHA)
        self.assertNotEqual(receipt["identity"]["derived"]["baseline"]["manifest_sha256"],
                            receipt["identity"]["derived"]["candidate"]["manifest_sha256"])

    def test_missing_bootstrap_still_fails_with_only_logical_tool_name(self):
        missing = self.state / "private-secret-path" / "compile"
        with self.assertRaisesRegex(base.GateError, "identity_file_missing"):
            runner.tool_hashes(self.state, {"go-compile": str(missing)})
        progress = json.loads((self.state / "progress.json").read_text())
        public = runner.export_progress(progress, None)
        self.assertEqual(public["current_tool"], "go-compile")
        self.assertNotIn("private-secret-path", json.dumps(public))
        with self.assertRaisesRegex(base.GateError, "tool_identity_labels"):
            runner.tool_hashes(self.state, {"/private/secret": str(missing)})
        with self.assertRaises(base.GateError):
            runner.export_progress(dict(progress, current_tool="/private/secret"), None)

    def test_helper_graph_binds_only_selected_goroot_files_without_executing(self):
        objects = helper_objects(self.go)
        with mock.patch.object(runner, "command", return_value=("\n".join(json.dumps(v) for v in objects), {})) as call:
            graph = runner.collect_helper_graph(self.state, self.go, {"GOPROXY": "off"}, self.budget, "prepare-test2json-graph")
        self.assertEqual(call.call_args.args[2], [str(self.go), "list", "-deps", "-json", "cmd/test2json"])
        self.assertNotIn("-race", call.call_args.args[2])
        self.assertEqual({p["id"] for p in graph["packages"]}, {"internal/test2json", "cmd/test2json"})
        self.assertNotIn(str(self.state), json.dumps(graph))
        self.assertNotIn("DO_NOT_EXPORT", json.dumps(graph))
        self.assertFalse(graph["native_include_closure_proven"])

    def test_helper_graph_rejects_external_module_escape_test_or_online_context(self):
        original = helper_objects(self.go)
        changes = [lambda p: p[0].update(Module={"Path": "example.com/unsafe"}),
                   lambda p: p[0].update(Goroot=False), lambda p: p[0].update(Dir=str(self.state)),
                   lambda p: p[0].update(GoFiles=["../outside.go"]),
                   lambda p: p[0].update(ForTest="unexpected"), lambda p: p[0].update(Error={"Err": "/private"}),
                   lambda p: p[1].update(Name="notmain"), lambda p: p[1]["Imports"].append("unselected/import"),
                   lambda p: p.append(dict(p[0]))]
        for change in changes:
            objects = json.loads(json.dumps(original))
            change(objects)
            with self.subTest(), mock.patch.object(runner, "command", return_value=("\n".join(json.dumps(v) for v in objects), {})), self.assertRaises(base.GateError):
                runner.collect_helper_graph(self.state, self.go, {"GOPROXY": "off"}, self.budget, "prepare-test2json-graph")
        with mock.patch.object(runner, "command") as command, self.assertRaises(base.GateError):
            runner.collect_helper_graph(self.state, self.go, {"GOPROXY": "https://proxy.golang.org"}, self.budget, "prepare-test2json-graph")
        command.assert_not_called()

    def test_tree_copy_does_not_modify_donor_and_binds_every_file(self):
        source = self.state / "baseline-prysm"
        original = runner.tree_manifest(source, self.budget.deadline)
        target = self.state / "copy"
        runner.copy_tree(source, target, original, self.budget.deadline)
        self.assertEqual(runner.tree_manifest(target, self.budget.deadline), original)
        self.assertEqual(runner.tree_manifest(source, self.budget.deadline), original)
        with self.assertRaises(base.GateError):
            runner.copy_tree(source, target, original, self.budget.deadline)

    def test_tree_rejects_symlinks_limits_and_expired_deadline(self):
        source = self.state / "baseline-prysm"
        (source / "linked").symlink_to(source / "go.mod")
        with self.assertRaises(base.GateError):
            runner.tree_manifest(source, self.budget.deadline)
        (source / "linked").unlink()
        (source / "linked").symlink_to(source / "crypto", target_is_directory=True)
        with self.assertRaises(base.GateError):
            runner.tree_manifest(source, self.budget.deadline)
        (source / "linked").unlink()
        for name, value in (("MAX_TREE_BYTES", 1), ("MAX_TREE_FILES", 1)):
            with mock.patch.object(runner, name, value), self.assertRaises(base.GateError):
                runner.tree_manifest(source, self.budget.deadline)
        with self.assertRaises(base.GateError):
            runner.tree_manifest(source, 0)

    def test_tree_deadline_is_checked_before_and_after_each_file(self):
        source = self.state / "one-file"
        source.mkdir()
        (source / "x.go").write_text("mock")
        for times, reads in (([0, 2], 0), ([0, 0, 2], 1)):
            with mock.patch.object(runner.time, "monotonic", side_effect=times), \
                 mock.patch.object(base, "read_file", wraps=base.read_file) as read, \
                 self.assertRaisesRegex(base.GateError, "source_deadline"):
                runner.tree_manifest(source, 1)
            self.assertEqual(read.call_count, reads)

    def test_safe_receipt_binds_exact_pretty_success_and_failure_bytes(self):
        for status in ("incomplete_not_acceptance", "target_red_and_six_candidate_passes"):
            payload = {"status": status, "mock_only": True}
            sink = io.StringIO()
            receipt = runner.emit_safe(payload, sink)
            first, printed = sink.getvalue().split("\n", 1)
            self.assertTrue(first.startswith("BLS_STAGE1_SAFE_RECEIPT "))
            self.assertEqual(json.loads(first.split(" ", 1)[1]), receipt)
            self.assertEqual(len(printed.encode()), receipt["bytes"])
            self.assertEqual(base.sha(printed.encode()), receipt["sha256"])
            self.assertEqual(json.loads(printed), payload)
            self.assertIn("\n  ", printed)
        with self.assertRaises(base.GateError):
            runner.emit_safe({"status": "/private/SENTINEL"}, io.StringIO())

    def test_graph_exact_sides_and_public_safe_metadata(self):
        fake = GraphFake(self.state, self.go)
        for side in runner.SIDES:
            with mock.patch.object(runner, "command", fake):
                graph = runner.collect_graph(self.state, side, self.go, {}, self.budget, "mock")
            text = json.dumps(graph)
            self.assertNotIn(str(self.state), text)
            self.assertNotIn("DO_NOT_EXPORT", text)
            self.assertFalse(graph["license_approval"])
            self.assertFalse(graph["native_include_closure_proven"])
            self.assertEqual(graph["nonblocking_selected"], side == "baseline")
            self.assertEqual(graph["hashicorp_selected"], side == "candidate")
            self.assertEqual(graph["modules"][base.BLS]["effective"]["kind"], "private_test_derived_source")
            self.assertEqual(graph["modules"][base.BLS]["download_origin"]["sum"], base.PINS[base.BLS][1])
            ids = {p["id"] for p in graph["packages"]}
            self.assertIn(runner.events.PACKAGE + " [" + runner.events.PACKAGE + ".test]", ids)
        self.assertTrue(all("-race" in args for label, args in fake.calls if label.endswith("-graph")))
        self.assertTrue(all(runner.events.PACKAGE in args for label, args in fake.calls if label.endswith("-graph")))

    def test_graph_rejects_errors_extra_tests_and_source_escape(self):
        changes = [lambda p: p[2].update(Error={"Err": "/private/source"}),
                   lambda p: p[2].update(ForTest=base.BLS + "/crypto/bls/blst"),
                   lambda p: p[2].update(Dir="/private/escape"),
                   lambda p: p[2].update(GoFiles=["../escape.go"]),
                   lambda p: p.append(dict(p[0])),
                   lambda p: p[2]["Imports"].append("unresolved/import"),
                   lambda p: p[3].update(TestGoFiles=["unselected.go"]),
                   lambda p: p[3]["Module"]["Replace"].update(Path="/private/other")]
        for change in changes:
            with self.subTest(), mock.patch.object(runner, "command", GraphFake(self.state, self.go, change)), self.assertRaises(base.GateError):
                runner.collect_graph(self.state, "baseline", self.go, {}, self.budget, "mock")

    def test_local_module_cannot_claim_official_effective_checksum(self):
        tree = self.state / "baseline-prysm"
        obj = {"Path": base.BLS, "Version": base.PINS[base.BLS][0], "Dir": str(tree),
               "Replace": {"Path": "../baseline-prysm", "Dir": str(tree)}}
        runner.local_prysm_module(obj, tree)
        for key in ("Sum", "GoModSum"):
            bad = json.loads(json.dumps(obj))
            bad["Replace"][key] = "h1:NOT-A-DERIVED-SUM"
            with self.assertRaises(base.GateError):
                runner.local_prysm_module(bad, tree)
        obj["Replace"]["Version"] = "v5.3.2"
        with self.assertRaises(base.GateError):
            runner.local_prysm_module(obj, tree)

    def test_generated_materialization_not_misrepresented_as_static_change(self):
        graph = {"packages": [{"id": "main", "files": [], "generated": {"materialized": False}}]}
        after = {"packages": [{"id": "main", "files": [], "generated": {"materialized": True, "sha256": "a" * 64}}]}
        self.assertEqual(runner.graph_fingerprint(graph), runner.graph_fingerprint(after))
        after["packages"][0]["files"] = [{"sha256": "b" * 64}]
        self.assertNotEqual(runner.graph_fingerprint(graph), runner.graph_fingerprint(after))

    def test_namespace_and_exact_test_commands(self):
        tools = {name: "/usr/bin/" + name for name in ("sudo", "timeout", "unshare", "setpriv", "prlimit", "gcc", "python")}
        rec = {"go": str(self.go), "uid": 1001, "gid": 1001, "tool_paths": tools}
        with mock.patch.object(os, "readlink", return_value="ns:[old]"):
            command = runner.isolated_command(self.repo, self.state, rec)
        for flag in ("--net", "--pid", "--mount-proc", "--kill-child=KILL", "--clear-groups",
                     "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs", "--core=0", "--nproc=256"):
            self.assertIn(flag, command)
        self.assertIn("GOPROXY=off", command)
        self.assertIn("GORACE=" + runner.events.GORACE, command)
        self.assertNotIn("--as", " ".join(command))
        for baseline in (True, False):
            argv = runner.test_command(self.state / "test2json", self.state / "example.test", baseline)
            self.assertEqual(argv[0], str(self.state / "test2json"))
            self.assertNotIn("tool", argv)
            self.assertIn("-test.count=1", argv)
            self.assertIn("-test.timeout=50s", argv)
            self.assertIn("-test.run=" + (runner.events.BASELINE_PATTERN if baseline else runner.events.CANDIDATE_PATTERN), argv)

    def test_fixed_offline_environment_cannot_inherit_gorace_or_network(self):
        with mock.patch.dict(os.environ, {"GORACE": "halt_on_error=1", "GITHUB_TOKEN": "secret", "GOPROXY": "https://elsewhere"}):
            env = runner.environment(self.go, self.state, True)
        self.assertEqual(env["GORACE"], runner.events.GORACE)
        self.assertEqual(env["GOPROXY"], "off")
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GOVULNDB", env)
        self.assertEqual(env["GOFLAGS"], base.FLAGS)

    def test_command_is_bounded_and_failure_cannot_be_test_red(self):
        (self.state / "logs").mkdir()
        with mock.patch.object(runner.process, "capture", return_value=(2, "private compiler error")) as capture:
            with self.assertRaisesRegex(base.GateError, "command_failed_mock-compile"):
                runner.command(self.state, "mock-compile", ["never-run"], {}, self.budget, 300)
            self.assertEqual(capture.call_args.args[-1], 300)
        self.assertEqual(runner.safe_failure(ValueError("/private/compiler")), "comparison_or_preparation_failed")

    def test_cli_rejects_unknown_arguments_without_go(self):
        with mock.patch.object(sys := runner.sys, "argv", ["runner", "--download-all"]), \
             mock.patch.object(runner, "run") as run, contextlib.redirect_stderr(io.StringIO()) as err, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(), 1)
        run.assert_not_called()
        self.assertEqual(err.getvalue(), "BLS_STAGE1_ERROR:unknown_stage1_argument\n")

    def inner_mock(self, outcomes, helper_failure=None):
        tools = {"gcc": "/mock/gcc", "ip": "/mock/ip"}
        graph = {"packages": [], "mock": "safe"}
        donor = {"manifest_sha256": "a" * 64, "file_count": 1, "bytes": 1}
        receipt = {"go": str(self.go), "uid": 1001, "gid": 1001, "tool_paths": tools,
                   "identity": {"mock": "identity"}, "graphs": {s: graph for s in runner.SIDES},
                   "helper_graph": {"packages": [], "mock": "helper"},
                   "donor": str(self.state / "donor"), "donor_manifest": donor}
        (self.state / "prepared.json").write_text(json.dumps(receipt))
        calls, helper_reads = [], [0]
        def helper_graph(*args, **kwargs):
            if helper_failure == "graph" and args[-1] == "inner-post-test2json-graph":
                return {"packages": [], "mock": "changed"}
            return receipt["helper_graph"]
        def binary_hash(path):
            if path.name == "test2json":
                helper_reads[0] += 1
                if helper_failure == "binary" and helper_reads[0] > 1:
                    return {"sha256": "d" * 64, "bytes": 3}
            return {"sha256": "b" * 64, "bytes": 3}
        def proof(code):
            return {"exit_code": code, "raw_private_bytes": 123, "raw_private_sha256": "c" * 64, "elapsed_milliseconds": 1}
        def command(state, label, args, env, budget, timeout=90, allow_failure=False, cwd=None):
            calls.append(label)
            if helper_failure == "compile" and label == "test2json-compile":
                raise base.GateError("command_failed_test2json-compile")
            if label.startswith("inner-"):
                return "[]", proof(0)
            if "compile" in label:
                return "", proof(0)
            if label.startswith("baseline-test-"):
                result = outcomes[int(label.rsplit("-", 1)[1]) - 1]
                if result == "error":
                    raise base.GateError("command_timeout")
                raw = stream(True, race_block() if result == "red" else None).replace(RACE_ROOT, str(state / "baseline-prysm"))
                return raw, proof(1 if result == "red" else 0)
            return stream(), proof(0)
        with mock.patch.object(runner, "command", command), mock.patch.object(runner, "collect_graph", return_value=graph), \
             mock.patch.object(runner, "verify_go"), mock.patch.object(runner, "identity", return_value=receipt["identity"]), \
             mock.patch.object(runner, "collect_helper_graph", side_effect=helper_graph), \
             mock.patch.object(runner, "hash_file", side_effect=binary_hash), \
             mock.patch.object(runner, "tree_manifest", return_value=donor), \
             mock.patch("elf_stack_policy.inspect_file", return_value={"sha256": "b" * 64, "policy_enforced": True,
                 "elf_class": 64, "byte_order": "little", "machine": 62, "elf_type": 2, "program_header_count": 9,
                 "gnu_stack_flags": 6, "load_flags": [4, 5, 6]}), \
             mock.patch.object(runner.isolation, "verify_isolation"), \
             mock.patch.object(os, "getuid", return_value=1001), mock.patch.object(os, "getgid", return_value=1001), \
             mock.patch.object(os, "readlink", return_value="ns:[new]"), \
             mock.patch.dict(os.environ, {"GORACE": runner.events.GORACE, "GOPROXY": "off", "BLS_STAGE1_HOST_PIDNS": "ns:[old]"}):
            runner.inner(self.repo, self.state)
        return calls

    def test_inner_both_builds_before_fresh_attempts_and_candidate(self):
        calls = self.inner_mock(["quiet", "quiet", "red"])
        self.assertLess(calls.index("test2json-compile"), calls.index("baseline-compile"))
        self.assertLess(calls.index("candidate-compile"), calls.index("baseline-test-1"))
        self.assertEqual([x for x in calls if x.startswith("baseline-test-")],
                         ["baseline-test-1", "baseline-test-2", "baseline-test-3"])
        self.assertEqual(calls[-1], "candidate-tests")
        report = json.loads((self.state / "result.json").read_text())
        self.assertEqual(len(report["baseline_attempts"]), 3)
        self.assertEqual(report["candidate"]["semantic"]["coverage"], "six_blackbox_tests")
        self.assertNotIn(str(self.state), json.dumps(report))

    def test_export_success_is_bound_to_host_receipt_and_exact_nested_shapes(self):
        self.inner_mock(["red"])
        value = json.loads((self.state / "result.json").read_text())
        receipt = json.loads((self.state / "prepared.json").read_text())
        checked = runner.export_success(value, receipt)
        self.assertEqual(checked["elf"]["candidate"]["gnu_stack_flags"], 6)
        self.assertNotIn(str(self.state), json.dumps(checked))
        mutations = [
            lambda v: v.update(raw="SENTINEL_PRIVATE_SOURCE"),
            lambda v: v["graphs"]["baseline"].update(raw="SENTINEL_PRIVATE_SOURCE"),
            lambda v: v["identity"].update(raw="SENTINEL_PRIVATE_SOURCE"),
            lambda v: v["candidate"]["execution"].update(raw="SENTINEL_PRIVATE_SOURCE"),
            lambda v: v["candidate"]["semantic"].update(raw="SENTINEL_PRIVATE_SOURCE"),
            lambda v: v["baseline_attempts"][0]["semantic"]["frame_classes"][0].update(raw="SENTINEL_PRIVATE_SOURCE"),
            lambda v: v["elf"]["candidate"].update(path="/private/SENTINEL_PRIVATE_SOURCE"),
            lambda v: v["policy"].update(raw="SENTINEL_PRIVATE_SOURCE"),
            lambda v: v["post_graph_sha256"].update(raw="SENTINEL_PRIVATE_SOURCE"),
            lambda v: v["binary_identity_before_and_after"]["candidate"].update(bytes=True),
            lambda v: v["helper_tool"].update(raw="SENTINEL_PRIVATE_SOURCE"),
            lambda v: v["helper_tool"]["graph"].update(raw="SENTINEL_PRIVATE_SOURCE"),
            lambda v: v["helper_tool"]["binary"].update(path="/private/SENTINEL"),
            lambda v: v["helper_tool"]["compile_receipt"].update(exit_code=1),
        ]
        for mutation in mutations:
            bad = json.loads(json.dumps(value))
            mutation(bad)
            with self.subTest(), self.assertRaises(base.GateError):
                runner.export_success(bad, receipt)

    def test_export_failure_progress_is_not_an_arbitrary_json_echo(self):
        self.inner_mock(["red"])
        progress = json.loads((self.state / "progress.json").read_text())
        receipt = json.loads((self.state / "prepared.json").read_text())
        progress["identity"] = receipt["identity"]
        runner.export_progress(progress, receipt)
        changes = [lambda v: v.update(raw="SENTINEL_PRIVATE_SOURCE"),
                   lambda v: v.update(current_command="SENTINEL_PRIVATE_SOURCE"),
                   lambda v: v["identity"].update(raw="SENTINEL_PRIVATE_SOURCE"),
                   lambda v: v["candidate"]["semantic"].update(raw="SENTINEL_PRIVATE_SOURCE")]
        for change in changes:
            bad = json.loads(json.dumps(progress))
            change(bad)
            with self.subTest(), self.assertRaises(base.GateError):
                runner.export_progress(bad, receipt)
        (self.state / "progress.json").write_text(json.dumps(dict(progress, unknown="SENTINEL_PRIVATE_SOURCE")))
        with self.assertRaisesRegex(base.GateError, "checkpoint_unknown_field"):
            runner.checkpoint(self.state, stage="candidate_executed")
        self.assertEqual(runner.safe_failure(base.GateError("SENTINEL_PRIVATE_SOURCE")), "comparison_or_preparation_failed")

    def test_outer_rejects_tampered_success_and_progress_without_exporting_sentinel(self):
        receipt = {"identity": {}, "tool_paths": {}, "go": "/mock/go"}
        def prepare(root, state, go):
            (state / "work").mkdir()
            (state / "progress.json").write_text(json.dumps({"raw": "SENTINEL_PRIVATE_SOURCE"}))
            return receipt
        def command(state, *args, **kwargs):
            (state / "result.json").write_text(json.dumps({"schema": "bls-stage1-private-comparison-v1",
                "status": "target_red_and_six_candidate_passes", "raw": "SENTINEL_PRIVATE_SOURCE"}))
            return "", {}
        out = io.StringIO()
        with mock.patch.object(runner.shutil, "which", return_value="/mock/go"), \
             mock.patch.object(runner, "prepare", prepare), mock.patch.object(runner, "command", command), \
             mock.patch.object(runner, "isolated_command", return_value=["never-execute"]), \
             mock.patch.object(runner, "identity", return_value={}), contextlib.redirect_stdout(out), \
             self.assertRaisesRegex(base.GateError, "stage1_comparison_failed"):
            runner.run(self.repo)
        self.assertNotIn("SENTINEL_PRIVATE_SOURCE", out.getvalue())
        self.assertIn("unsafe_progress_rejected", out.getvalue())
        self.assertIn('\n  "', out.getvalue())

    def test_malformed_private_progress_is_redacted_but_still_receipted(self):
        receipt = {"identity": {}, "tool_paths": {}, "go": "/mock/go"}
        for filename, text in (("progress.json", '{"raw":"SENTINEL_PRIVATE_SOURCE'),
                               ("progress.json", '["SENTINEL_PRIVATE_SOURCE"]'),
                               ("failure.json", '{"raw":"SENTINEL_PRIVATE_SOURCE'),
                               ("failure.json", '["SENTINEL_PRIVATE_SOURCE"]')):
            def prepare(root, state, go):
                (state / "work").mkdir()
                (state / filename).write_text(text)
                return receipt
            def command(*args, **kwargs):
                raise base.GateError("command_failed_outer-namespace")
            sink = io.StringIO()
            with self.subTest(filename=filename, text=text), \
                 mock.patch.object(runner.shutil, "which", return_value="/mock/go"), \
                 mock.patch.object(runner, "prepare", prepare), mock.patch.object(runner, "command", command), \
                 mock.patch.object(runner, "isolated_command", return_value=["never-execute"]), \
                 contextlib.redirect_stdout(sink), self.assertRaises(base.GateError):
                runner.run(self.repo)
            line, raw = sink.getvalue().split("\n", 1)
            self.assertNotIn("SENTINEL", raw)
            self.assertEqual(json.loads(raw)["gate_code"], "unsafe_progress_rejected")
            self.assertEqual(json.loads(line.split(" ", 1)[1])["sha256"], base.sha(raw.encode()))

    def test_helper_build_or_post_identity_failure_cannot_be_target_red(self):
        for failure, code in (("compile", "command_failed_test2json-compile"),
                              ("graph", "helper_graph_changed"), ("binary", "helper_binary_changed")):
            with self.subTest(failure=failure), self.assertRaisesRegex(base.GateError, "^" + code + "$"):
                self.inner_mock(["red"], helper_failure=failure)
            self.assertFalse((self.state / "result.json").exists())

    def test_inner_stops_after_first_valid_red(self):
        calls = self.inner_mock(["red"])
        self.assertEqual([x for x in calls if x.startswith("baseline-test-")], ["baseline-test-1"])

    def test_inner_three_quiet_passes_are_not_reproduction(self):
        with self.assertRaisesRegex(base.GateError, "baseline_not_reproduced"):
            self.inner_mock(["quiet", "quiet", "quiet"])
        self.assertFalse((self.state / "result.json").exists())
        progress = json.loads((self.state / "progress.json").read_text())
        self.assertEqual(len(progress["baseline_attempts"]), 3)
        self.assertNotIn("candidate", progress)

    def test_inner_timeout_is_not_retried_as_quiet(self):
        with self.assertRaisesRegex(base.GateError, "command_timeout"):
            self.inner_mock(["error"])
        self.assertFalse((self.state / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
