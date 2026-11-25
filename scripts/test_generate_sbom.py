# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import copy
import errno
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import generate_sbom
from generate_sbom import (ADAPTER, MODULE, REPOSITORY, build_environment,
                           normalize, validate_output, verify_modules, verify_origin)


def fixture():
    return {"metadata": {"component": {"name": MODULE, "bom-ref": "root"}},
            "components": [{"name": ADAPTER, "bom-ref": "old", "version": "old"},
                           {"name": "github.com/pion/stun/v3", "bom-ref": "v3"}],
            "dependencies": [{"ref": "root", "dependsOn": ["old"]},
                             {"ref": "old", "dependsOn": ["v3"]}]}


class NormalizeSBOMTest(unittest.TestCase):
    def test_local_adapter_is_not_misattributed_to_upstream(self):
        sha = "1" * 40
        result = normalize(fixture(), sha)
        validate_output(result)
        component = result["components"][0]
        self.assertEqual(component["version"], sha)
        self.assertIn("compatibility adapter", component["name"])
        self.assertEqual(result["dependencies"][0]["dependsOn"], [component["bom-ref"]])
        self.assertEqual(result["dependencies"][1]["ref"], component["bom-ref"])
        self.assertEqual(component["licenses"][0]["license"]["id"], "GPL-3.0-or-later")
        self.assertIn(sha + "/compat/stunv2", component["externalReferences"][0]["url"])

    def test_rejects_non_commit_identity(self):
        with self.assertRaises(ValueError):
            normalize({}, "main")

    def test_rejects_unexpected_identity_or_missing_adapter(self):
        for bom in ({"metadata": {"component": {"name": "another-project"}}},
                    {"metadata": {"component": {"name": MODULE}}, "components": []}):
            with self.assertRaises(ValueError):
                normalize(bom, "1" * 40)

    def test_requires_repository_identity(self):
        verify_origin(REPOSITORY)
        verify_origin(REPOSITORY + ".git")
        for origin in ("/tmp/source", REPOSITORY + "-other", "https://other.example/repo", "https://secret@github.com/keienWang/bsc-p2p-relay"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                verify_origin(origin)

    def test_requires_real_in_tree_module_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            rootdir = pathlib.Path(temp)
            adapterdir = rootdir / "compat/stunv2"
            adapterdir.mkdir(parents=True)
            root = {"Path": MODULE, "Main": True, "Dir": str(rootdir)}
            adapter = {"Path": ADAPTER, "Replace": {"Path": "./compat/stunv2", "Dir": str(adapterdir), "GoMod": str(adapterdir / "go.mod")}}
            verify_modules(root, adapter, rootdir)
            for bad in ({"Path": ADAPTER, "Version": "v2.0.0"},
                        {"Path": ADAPTER, "Replace": {"Path": "../outside", "Dir": "/tmp/outside"}},
                        {"Path": ADAPTER, "Replace": {"Path": "./compat/stunv2", "Dir": str(adapterdir), "GoMod": str(adapterdir / "go.mod"), "Version": "v2.0.0"}}):
                with self.subTest(adapter=bad), self.assertRaises(ValueError):
                    verify_modules(root, bad, rootdir)
            with self.assertRaises(ValueError):
                verify_modules(dict(root, Path="other-project"), adapter, rootdir)

    def test_disables_workspace_and_persisted_source_overrides(self):
        env = build_environment({"GOWORK": "/tmp/foreign.work", "GOENV": "/tmp/foreign.env", "GOFLAGS": "-buildvcs=false", "GOOS": "linux", "GOARCH": "arm64", "CGO_ENABLED": "0"})
        self.assertEqual(env["GOWORK"], "off")
        self.assertEqual(env["GOENV"], "off")
        self.assertEqual(env["GOFLAGS"], "-buildvcs=false -mod=readonly")
        self.assertEqual(env["GOARCH"], "arm64")
        self.assertEqual(env["CGO_ENABLED"], "0")
        for flags in ("-overlay=/tmp/other.json", "-modfile=else.mod", "-mod=vendor", "-tags=custom"):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                build_environment({"GOFLAGS": flags})
        with self.assertRaises(ValueError):
            build_environment({"GOEXPERIMENT": "something"})

    def test_rejects_dangling_or_duplicate_references(self):
        for change in (lambda b: b["dependencies"][0]["dependsOn"].append("absent"),
                       lambda b: b["dependencies"].append({"ref": "absent"}),
                       lambda b: b["components"].append(copy.deepcopy(b["components"][0])),
                       lambda b: b["dependencies"].append(copy.deepcopy(b["dependencies"][0]))):
            bom = fixture()
            change(bom)
            with self.assertRaises(ValueError):
                validate_output(bom)

    def test_rejects_local_locations_and_url_credentials(self):
        for value in ("/some/local/path", "/workspace/source", "C:\\private\\source", "file:///private/source", "https://token@example.com/repo", "https://example.com/repo?token=secret", "https://example.com/%2Ftmp%2Fsource"):
            bom = fixture()
            bom["metadata"]["properties"] = [{"name": "test", "value": value}]
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_output(bom)


class SBOMPublicationTest(unittest.TestCase):
    def invoke(self, output, *, on_generate=None):
        # Exercise the production command's publication path; stub only the
        # Git/Go/generator tool boundary, so no process reaches a registry.
        def tool_output(command, **_):
            if command[0] == "git" and command[3:] == ["remote", "get-url", "origin"]:
                return REPOSITORY
            if command[0] == "git" and command[3:5] == ["rev-parse", "--verify"]:
                return "1" * 40
            if command[0] == "git" and command[3] == "status":
                return ""
            if command == ["go", "env", "-json", "GOHOSTOS", "GOHOSTARCH"]:
                return '{"GOHOSTOS":"linux","GOHOSTARCH":"amd64"}'
            raise AssertionError(f"unexpected fixture command: {command}")

        def tool_run(command, **_):
            if pathlib.Path(command[0]).name == "cyclonedx-gomod":
                pathlib.Path(command[command.index("-output") + 1]).write_text(
                    json.dumps(dict(fixture(), specVersion="1.6")), encoding="utf-8")
                if on_generate is not None:
                    on_generate()
            elif command[0] not in ("git", "go"):
                raise AssertionError(f"unexpected fixture command: {command}")
            return subprocess.CompletedProcess(command, 0)

        with (mock.patch.object(sys, "argv", ["generate_sbom.py", "--output", str(output)]),
              mock.patch.object(generate_sbom, "verify_checkout"),
              mock.patch.object(generate_sbom.subprocess, "check_output", side_effect=tool_output),
              mock.patch.object(generate_sbom.subprocess, "run", side_effect=tool_run),
              contextlib.redirect_stdout(io.StringIO())):
            generate_sbom.main()

    def test_partial_stage_write_preserves_prior_sbom(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            target = root / "manifest.json"
            original = b'{"previous":true}\n'
            target.write_bytes(original)
            real_open = pathlib.Path.open

            def fail_after_prefix(path, mode="r", *args, **kwargs):
                if mode == "w" and path.name == target.name and root in path.parents:
                    with real_open(path, mode, *args, **kwargs) as handle:
                        handle.write("{")
                    raise OSError(errno.ENOSPC, "fixture disk full after partial write")
                return real_open(path, mode, *args, **kwargs)

            with (mock.patch.object(pathlib.Path, "open", fail_after_prefix),
                  self.assertRaisesRegex(OSError, "fixture disk full")):
                self.invoke(target)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(root.iterdir()), [target])

    def test_failed_atomic_replace_preserves_prior_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            target = root / "manifest.json"
            target.write_bytes(b"prior manifest")
            with (mock.patch.object(generate_sbom.os, "replace",
                                    side_effect=OSError(errno.EIO, "fixture publication error")),
                  self.assertRaisesRegex(OSError, "fixture publication error")):
                self.invoke(target)
            self.assertEqual(target.read_bytes(), b"prior manifest")
            self.assertEqual(list(root.iterdir()), [target])

    def test_successful_publish_creates_and_replaces_a_complete_sbom(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            target = root / "new" / "manifest.json"
            self.invoke(target)
            first = target.read_bytes()
            self.assertEqual(json.loads(first)["specVersion"], "1.6")
            target.write_bytes(b"prior manifest")
            target.chmod(0o640)
            self.invoke(target)
            self.assertEqual(target.read_bytes(), first)
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_does_not_replace_an_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory).resolve() / "existing"
            target.mkdir()
            with self.assertRaisesRegex(ValueError, "SBOM output must be a regular file"):
                self.invoke(target)
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])

    def test_cli_rejects_existing_and_dangling_output_symlinks_before_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            old = root / "previous.json"
            old.write_bytes(b"prior manifest")
            for referent in (old, root / "missing.json"):
                with self.subTest(referent=referent.name):
                    target = root / f"linked-{referent.name}"
                    target.symlink_to(referent.name)
                    with (mock.patch.object(sys, "argv", ["generate_sbom.py", "--output", str(target)]),
                          mock.patch.object(generate_sbom.subprocess, "check_output",
                                            side_effect=AssertionError("must reject before running tools")),
                          self.assertRaisesRegex(ValueError, "SBOM output must be a regular file")):
                        generate_sbom.main()
                    self.assertEqual(target.readlink(), pathlib.Path(referent.name))
            self.assertEqual(old.read_bytes(), b"prior manifest")
            self.assertFalse((root / "missing.json").exists())

    def test_cli_refuses_a_link_created_during_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target, prior = root / "manifest.json", root / "prior.json"
            prior.write_bytes(b"another manifest")
            with self.assertRaisesRegex(ValueError, "SBOM output must be a regular file"):
                self.invoke(target, on_generate=lambda: target.symlink_to(prior.name))
            self.assertEqual(target.readlink(), pathlib.Path(prior.name))
            self.assertEqual(prior.read_bytes(), b"another manifest")
            self.assertEqual(set(root.iterdir()), {target, prior})

    def test_cli_still_accepts_a_regular_file_inside_a_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            actual = root / "actual"
            actual.mkdir()
            (root / "alias").symlink_to(actual.name, target_is_directory=True)
            self.invoke(root / "alias" / "manifest.json")
            target = actual / "manifest.json"
            self.assertEqual(json.loads(target.read_text())["specVersion"], "1.6")
            self.assertTrue((root / "alias").is_symlink())


if __name__ == "__main__":
    unittest.main()
