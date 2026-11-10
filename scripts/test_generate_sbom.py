# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import pathlib
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
