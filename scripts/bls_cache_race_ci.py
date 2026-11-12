#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private, bounded Stage1 orchestration. No production dependency is modified.

Local validation is Python-only. Hosted execution requires a reviewed workflow;
no local contract result is evidence of Go/native execution or race reproduction.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

import bls_stage0_inventory as base
import bls_cache_race_evidence as events
import bls_metadata_capture as metadata_capture
import scanner_redgreen_ci as process
import scanner_synthetic_ci as isolation

require = base.require
SIDES = ("baseline", "candidate")
MATERIAL = "research/bls-cache-race"
PATCH = "0001-use-bounded-synchronous-blst-cache.patch"
FIXTURE = "cachepatchtests/public_key_cache_blackbox_test.go"
PATCH_SHA = "28ac9e83965e456c05acfc49ddaf59c311616fdd7d24e1db215be98a03bfc6d6"
FIXTURE_SHA = "9f3f83750c7425b6159c27d7cdafc0d17198c9205a5517028448526e9a3236ab"
STAGE0_SHA = "76938fd9d47a5552f310d52827e7e76f99675e2eca07000a278e4a7261a1067f"
PREPARE_SECONDS = 360
INNER_SECONDS = 900
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_TREE_FILES = 20000
MAX_BINARY_BYTES = 256 * 1024 * 1024
MAX_REPORT_BYTES = 4 * 1024 * 1024
TOOL_LABELS = {"go", "python", "goroot_version", "git", "gcc", "as", "ld", "sudo", "timeout",
               "unshare", "setpriv", "ip", "prlimit", "go-asm", "go-compile", "go-link", "go-cgo"}
HELPER_PACKAGE = "cmd/test2json"
RUNNER_FILES = ("scripts/bls_cache_race_ci.py", "scripts/bls_cache_race_evidence.py",
                "scripts/bls_stage0_inventory.py", "scripts/scanner_redgreen_ci.py",
                "scripts/scanner_synthetic_ci.py", "scripts/elf_stack_policy.py", "scripts/bls_metadata_capture.py")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def hash_file(path, maximum=MAX_BINARY_BYTES):
    require(path.is_file() and not path.is_symlink(), "identity_file_missing")
    require(path.stat().st_size <= maximum, "identity_file_limit")
    digest, total = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while part := handle.read(1024 * 1024):
            total += len(part)
            require(total <= maximum, "identity_file_limit")
            digest.update(part)
    return {"sha256": digest.hexdigest(), "bytes": total}


def tool_hashes(state, paths):
    require(isinstance(paths, dict) and set(paths) <= TOOL_LABELS, "tool_identity_labels")
    result = {}
    for name, path in paths.items():
        checkpoint(state, current_tool=name)
        result[name] = hash_file(Path(path))
    return result


def tree_manifest(root, deadline):
    require(root.is_dir() and not root.is_symlink(), "source_root_invalid")
    files, total = {}, 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        require(time.monotonic() < deadline, "source_deadline")
        current = Path(directory)
        require(all(not (current / d).is_symlink() for d in dirs), "source_directory_symlink")
        for name in sorted(names):
            require(time.monotonic() < deadline, "source_deadline")
            relative = str((current / name).relative_to(root))
            base.relative(relative)
            data = base.read_file(root, relative)
            total += len(data)
            require(total <= MAX_TREE_BYTES and len(files) < MAX_TREE_FILES, "source_tree_limit")
            files[relative] = base.sha(data)
            require(time.monotonic() < deadline, "source_deadline")
    require(files, "empty_source_tree")
    return {"files": files, "bytes": total, "file_count": len(files),
            "manifest_sha256": base.sha(canonical(files))}


def copy_tree(source, target, manifest, deadline):
    require(not target.exists(), "derived_source_exists")
    target.mkdir(mode=0o700)
    for relative, digest in manifest["files"].items():
        require(time.monotonic() < deadline, "source_deadline")
        data = base.read_file(source, relative)
        require(base.sha(data) == digest, "donor_changed_during_copy")
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    require(tree_manifest(target, deadline) == manifest, "source_copy_mismatch")


def verify_material(root):
    bundle = root / MATERIAL
    manifest = json.loads(base.read_file(bundle, "MANIFEST.json"))
    expected = {PATCH, "blackbox_test.go.txt", "NOTICE.md", "UPSTREAM.json", "README.md"}
    require({p.name for p in bundle.iterdir()} == expected | {"MANIFEST.json"}, "material_scope")
    require(len(manifest["files"]) == len(expected) and
            {e["path"] for e in manifest["files"]} == expected, "material_manifest_scope")
    for entry in manifest["files"]:
        data = base.read_file(bundle, entry["path"])
        require(base.sha(data) == entry["sha256"] and len(data) == entry["bytes"], "material_hash_mismatch")
    require(base.sha(base.read_file(bundle, PATCH)) == PATCH_SHA and
            base.sha(base.read_file(bundle, "blackbox_test.go.txt")) == FIXTURE_SHA, "material_pin_mismatch")
    require(manifest["blackbox"]["expected_tests"] == list(events.TESTS) and
            manifest["blackbox"]["package_argument"] == events.PACKAGE and
            manifest["blackbox"]["run_expression"] == events.CANDIDATE_PATTERN, "material_test_scope")
    require(base.sha(base.read_file(root, "LICENSE")) == base.INPUTS["LICENSE"], "license_input_mismatch")
    return {e["path"]: e["sha256"] for e in manifest["files"]} | {
        "MANIFEST.json": base.sha(base.read_file(bundle, "MANIFEST.json"))}


def work_inputs(root, work):
    inputs = base.input_hashes(root)
    work.mkdir(mode=0o700)
    for name in base.INPUTS:
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base.read_file(root, name))
    return inputs


def make_modfile(work, side):
    require(side in SIDES, "unknown_side")
    original = base.read_file(work, "go.mod")
    require(b"replace " + base.BLS.encode() not in original, "existing_prysm_replacement")
    data = original + ("\nreplace " + base.BLS + " => ../" + side + "-prysm\n").encode()
    (work / (side + ".mod")).write_bytes(data)
    (work / (side + ".sum")).write_bytes(base.read_file(work, "go.sum"))
    return {side + ".mod": base.sha(data), side + ".sum": base.sha(base.read_file(work, "go.sum"))}


def environment(go, state, offline):
    env = base.clean_env(go, state)
    env.update(PATH=str(go.parent) + ":/usr/bin:/bin:/usr/sbin:/sbin", GOPROXY="off" if offline else "https://proxy.golang.org",
               GORACE=events.GORACE)
    return env


def command(state, label, args, env, budget, timeout=90, allow_failure=False, cwd=None):
    require(re.fullmatch(r"[a-z0-9-]+", label), "command_label")
    checkpoint(state, current_command=label)
    started = time.monotonic()
    separate = label in METADATA_LABELS
    capture = metadata_capture.capture if separate else process.capture
    try:
        code, output = capture(args, cwd or state / "work", env, state / "logs", label, budget, timeout)
    except Exception:
        status_path = state / "logs" / (label + ".json")
        if status_path.exists():
            if separate:
                status = metadata_capture.read_status(status_path.parent, label)
                checkpoint(state, command_failure={"label": label, "reason": status["reason"],
                    "capture_mode": metadata_capture.MODE, "stdout_private": status["stdout_private"],
                    "stderr_private": status["stderr_private"]})
            else:
                status = json.loads(base.read_file(status_path.parent, status_path.name))
                reason = status.get("reason")
                require(reason in (None, "timeout", "output_limit", "termination_permission_error"), "capture_reason")
                digest = hash_file(state / "logs" / (label + ".log"), process.COMMAND_BYTES)
                require(status.get("bytes") == digest["bytes"] and status.get("sha256") == digest["sha256"], "capture_status_identity")
                checkpoint(state, command_failure={"label": label, "reason": reason,
                                                   "raw_private_bytes": digest["bytes"], "raw_private_sha256": digest["sha256"]})
        raise base.GateError("command_capture_failed_" + label) from None
    elapsed = int((time.monotonic() - started) * 1000)
    if separate:
        status = metadata_capture.read_status(state / "logs", label)
        require(status["reason"] is None and status["returncode"] == code, "metadata_success_status")
        parsed_bytes = output.encode("utf-8", errors="strict")
        require(status["stdout_private"] == {"bytes": len(parsed_bytes), "sha256": base.sha(parsed_bytes)},
                "metadata_stdout_identity")
        receipt = {"exit_code": code, "capture_mode": metadata_capture.MODE,
                   "stdout_private": status["stdout_private"], "stderr_private": status["stderr_private"],
                   "elapsed_milliseconds": elapsed}
    else:
        receipt = {"exit_code": code, "raw_private_bytes": len(output.encode()),
                   "raw_private_sha256": base.sha(output.encode()), "elapsed_milliseconds": elapsed}
    checkpoint(state, last_command={"label": label, **receipt})
    if not allow_failure:
        require(code == 0, "command_failed_" + label)
    return output, receipt


def package_id(value):
    if value == events.PACKAGE + " [" + events.PACKAGE + ".test]":
        return value
    return base.module_name(value)


def local_prysm_module(obj, tree):
    require(obj.get("Path") == base.BLS and obj.get("Version") == base.PINS[base.BLS][0] and
            not obj.get("Main") and not obj.get("Error"), "local_prysm_declared_identity")
    replacement = obj.get("Replace")
    require(isinstance(replacement, dict) and replacement.get("Path") == "../" + tree.name and
            not replacement.get("Version") and not replacement.get("Replace") and not replacement.get("Error") and
            not replacement.get("Main") and replacement.get("Dir") == str(tree) and obj.get("Dir") == str(tree),
            "local_prysm_effective_identity")
    require(not any(obj.get(k) or replacement.get(k) for k in ("Sum", "GoModSum")), "local_prysm_false_checksum")


def collect_graph(state, side, go, env, budget, stage):
    """Exact six-test -race declarations, not a complete C include closure."""
    tree, work = state / (side + "-prysm"), state / "work"
    flags = ["-modfile=" + side + ".mod", "-race"]
    raw, _ = command(state, stage + "-" + side + "-graph", [str(go), "list", *flags, "-deps", "-test", "-json", events.PACKAGE], env, budget)
    packages = base.stream_json(raw)
    used = {base.module_name(p["Module"]["Path"]) for p in packages if p.get("Module")}
    require(base.BLS in used and base.BLST in used, "missing_race_modules")
    require(base.MAIN not in used, "unexpected_relay_package")
    raw, _ = command(state, stage + "-" + side + "-modules", [str(go), "list", "-modfile=" + side + ".mod", "-m", "-json", *sorted(used)], env, budget)
    objects = base.stream_json(raw)
    require(len(objects) == len(used) and {m.get("Path") for m in objects} == used, "race_module_inventory")
    sums = base.sum_entries(base.read_file(work, "go.sum").decode())
    modules, roots = {}, {}
    for obj in objects:
        name = obj["Path"]
        if name == base.BLS:
            local_prysm_module(obj, tree)
            version, checksum, modsum = base.PINS[base.BLS]
            modules[name] = {"download_origin": {"path": name, "version": version, "sum": checksum, "go_mod_sum": modsum},
                             "effective": {"kind": "private_test_derived_source", "side": side,
                                           "manifest_sha256": tree_manifest(tree, budget.deadline)["manifest_sha256"]}}
            roots[name] = tree
        else:
            modules[name], roots[name] = base.module_record(obj, sums, state)
    records, directories = [], {k: {v} for k, v in roots.items()}
    goroot = go.parent.parent
    directories["std"] = {goroot}
    seen, file_count, source_budget = set(), 0, {"bytes": 0}
    for obj in packages:
        require(not obj.get("Error") and not obj.get("DepsErrors") and not obj.get("Incomplete"), "race_package_error")
        name = package_id(obj.get("ImportPath"))
        require(name not in seen, "duplicate_race_package")
        seen.add(name)
        require(obj.get("ForTest", "") in ("", events.PACKAGE), "upstream_tests_selected")
        imports = [package_id(x) for x in obj.get("Imports", [])]
        mapping = {package_id(k): package_id(v) for k, v in obj.get("ImportMap", {}).items()}
        rec = {"id": name, "for_test": obj.get("ForTest", ""), "imports": imports, "import_map": mapping}
        if name == events.PACKAGE + ".test":
            require(obj.get("Name") == "main" and len(obj.get("GoFiles", [])) == 1, "race_testmain_shape")
            generated = Path(obj["GoFiles"][0])
            require(generated.name == "_testmain.go" or re.fullmatch(r"[0-9a-f]{64}-d", generated.name), "race_generated_name")
            if not generated.is_absolute() and obj.get("Dir"):
                generated = Path(obj["Dir"]) / generated
            if generated.is_file():
                require(generated.is_relative_to(state) and not generated.is_symlink(), "race_generated_escape")
                digest = base.file_record(state, generated, source_budget, budget.deadline)["sha256"]
                rec["generated"] = {"logical_name": "_testmain.go", "materialized": True, "sha256": digest}
            else:
                require(str(generated) == "_testmain.go" or generated.is_absolute() and generated.is_relative_to(state), "race_generated_missing")
                rec["generated"] = {"logical_name": "_testmain.go", "materialized": False, "sha256": None}
            rec.update(component="testmain", files=[])
            records.append(rec)
            continue
        if obj.get("Standard"):
            component, root = "std", goroot
        else:
            module = obj.get("Module", {})
            component = base.module_name(module.get("Path"))
            require(component in roots, "race_unknown_module")
            root = roots[component]
            if component == base.BLS:
                local_prysm_module(module, tree)
            else:
                base.check_package_module(module, modules[component], root)
        directory = Path(obj.get("Dir", "")).resolve()
        require(directory.is_dir() and directory.is_relative_to(root), "race_package_dir_escape")
        directories[component].add(directory)
        fields = list(base.FILE_FIELDS)
        if name == events.PACKAGE:
            require(obj.get("TestGoFiles") == [Path(FIXTURE).name] and not obj.get("XTestGoFiles"), "race_fixture_files")
            fields.append("TestGoFiles")
        files = []
        for field in fields:
            for relative in obj.get(field, []):
                path = directory / base.relative(relative)
                item = base.file_record(root, path, source_budget, budget.deadline)
                item["role"] = field
                files.append(item)
                file_count += 1
                require(file_count <= 4096, "race_source_file_limit")
        require(files, "race_package_sources_missing")
        rec.update(component=component, files=files)
        records.append(rec)
    require(all(n in seen for n in (events.PACKAGE, events.PACKAGE + ".test", events.PACKAGE + " [" + events.PACKAGE + ".test]")), "race_test_instances_missing")
    require(base.BLS + "/crypto/bls" not in seen and not any(x.startswith(base.BLS + "/crypto/bls/herumi") for x in seen), "unexpected_bls_wrapper")
    nonblocking = base.BLS + "/cache/nonblocking" in seen
    hashicorp = base.LRU in seen
    require((nonblocking, hashicorp) == ((True, False) if side == "baseline" else (False, True)), "cache_consumer_graph_mismatch")
    for rec in records:
        require(all(rec["import_map"].get(x, x) == "C" or rec["import_map"].get(x, x) in seen for x in rec["imports"]), "race_import_unresolved")
    all_roots = dict(roots, std=goroot)
    licenses = {k: base.found_licenses(all_roots[k], v, source_budget, budget.deadline) for k, v in directories.items()}
    source_trees = {name: safe_tree(tree_manifest(path, budget.deadline)) for name, path in roots.items()}
    return {"scope": "six_blackbox_race_package_declarations", "side": side, "modules": modules,
            "module_source_trees": source_trees,
            "packages": records, "found_license_files": licenses, "license_approval": False,
            "native_include_closure_proven": False, "nonblocking_selected": nonblocking,
            "hashicorp_selected": hashicorp}


def collect_helper_graph(state, go, env, budget, label):
    require(env.get("GOPROXY") == "off", "helper_requires_offline_resolution")
    raw, _ = command(state, label, [str(go), "list", "-deps", "-json", HELPER_PACKAGE], env, budget, 90)
    objects = base.stream_json(raw)
    root = go.parent.parent
    seen, records, directories = set(), [], {root}
    file_budget, count = {"bytes": 0}, 0
    for obj in objects:
        require(not obj.get("Module") and obj.get("Goroot") is True and not obj.get("ForTest") and
                not obj.get("Error") and not obj.get("DepsErrors") and not obj.get("Incomplete"), "helper_package_origin")
        name = base.module_name(obj.get("ImportPath"))
        require(name not in seen, "helper_duplicate_package")
        seen.add(name)
        directory = Path(obj.get("Dir", "")).resolve()
        require(directory.is_dir() and directory.is_relative_to(root / "src"), "helper_source_escape")
        directories.add(directory)
        imports = [base.module_name(x) for x in obj.get("Imports", [])]
        mapping = {base.module_name(k): base.module_name(v) for k, v in obj.get("ImportMap", {}).items()}
        files = []
        for field in base.FILE_FIELDS:
            for relative in obj.get(field, []):
                item = base.file_record(root, directory / base.relative(relative), file_budget, budget.deadline)
                item["role"] = field
                files.append(item)
                count += 1
                require(count <= 4096, "helper_source_limit")
        require(files, "helper_sources_missing")
        if name == HELPER_PACKAGE:
            require(obj.get("Name") == "main", "helper_not_command")
        records.append({"id": name, "imports": imports, "import_map": mapping, "files": files})
    require(HELPER_PACKAGE in seen, "helper_command_missing")
    for rec in records:
        require(all(rec["import_map"].get(x, x) == "C" or rec["import_map"].get(x, x) in seen for x in rec["imports"]), "helper_unresolved_import")
    return {"scope": "goroot_test2json_nonrace_declarations", "package": HELPER_PACKAGE,
            "packages": records, "found_license_files": base.found_licenses(root, directories, file_budget, budget.deadline),
            "native_include_closure_proven": False}


def graph_fingerprint(graph):
    # Generated testmain can become materialized after compilation. Keep that
    # evidence in each inventory, but compare every static declaration exactly.
    copy = json.loads(json.dumps(graph))
    for package in copy["packages"]:
        package.pop("generated", None)
    return base.sha(canonical(copy))


def safe_tree(manifest):
    return {k: manifest[k] for k in ("manifest_sha256", "file_count", "bytes")}


def identity(root, state, tool_paths, deadline):
    return {"repository_inputs": base.input_hashes(root), "material": verify_material(root),
            "runner_sources": {name: base.sha(base.read_file(root, name)) for name in RUNNER_FILES},
            "tools": tool_hashes(state, tool_paths),
            "derived": {side: safe_tree(tree_manifest(state / (side + "-prysm"), deadline)) for side in SIDES},
            "private_modfiles": {side + extension: base.sha(base.read_file(state / "work", side + extension))
                                 for side in SIDES for extension in (".mod", ".sum")},
            "copied_inputs": {name: base.sha(base.read_file(state / "work", name)) for name in base.INPUTS}}


def verify_go(state, go, env, budget, label):
    raw, _ = command(state, label, [str(go), "env", "-json", *base.ENV_KEYS], env, budget)
    objects = base.stream_json(raw)
    require(len(objects) == 1 and set(objects[0]) == set(base.ENV_KEYS), "go_environment_shape")
    info = objects[0]
    expected = {"GOVERSION": base.GO_VERSION, "GOOS": "linux", "GOARCH": "amd64", "GOAMD64": "v1",
                "GOHOSTOS": "linux", "GOHOSTARCH": "amd64", "CGO_ENABLED": "1",
                "GOMOD": str(state / "work/go.mod"), "GOWORK": "off", "GOFLAGS": base.FLAGS,
                "GOPROXY": env["GOPROXY"], "GOSUMDB": "off", "GOTOOLCHAIN": "local",
                "GOROOT": str(go.parent.parent), "GOMODCACHE": str(state / "modcache"),
                "GOCACHE": str(state / "buildcache")}
    require(all(info.get(k) == v or k == "GOWORK" and info.get(k) == "" for k, v in expected.items()), "stage1_go_environment")
    version = base.read_file(go.parent.parent, "VERSION").decode().splitlines()
    require(version and version[0] == base.GO_VERSION, "stage1_goroot_version")


def prepare(root, state, go):
    require(sys.platform == "linux" and os.geteuid() != 0, "ordinary_linux_user_required")
    base.reject_environment(os.environ)
    budget = process.Budget(PREPARE_SECONDS, 64 * 1024 * 1024)
    material = verify_material(root)
    for name in ("home", "tmp", "modcache", "buildcache", "logs"):
        (state / name).mkdir(mode=0o700)
    work_inputs(root, state / "work")
    tool_paths = {"go": str(go), "python": str(Path(sys.executable).resolve()),
                  "goroot_version": str(go.parent.parent / "VERSION")}
    for name in ("git", "gcc", "as", "ld", "sudo", "timeout", "unshare", "setpriv", "ip", "prlimit"):
        checkpoint(state, current_tool=name)
        value = shutil.which(name)
        require(value is not None, "missing_platform_tool")
        tool_paths[name] = str(Path(value).resolve())
    for name in ("asm", "compile", "link", "cgo"):
        tool_paths["go-" + name] = str(go.parent.parent / "pkg/tool/linux_amd64" / name)
    before_tools = tool_hashes(state, tool_paths)
    env = environment(go, state, False)
    env["CC"] = tool_paths["gcc"]
    verify_go(state, go, env, budget, "prepare-go-env")
    # Go 1.26 binary distributions omit standalone test2json. Metadata only
    # here; its explicit build happens later inside the offline namespace.
    helper_env = dict(env, GOPROXY="off")
    helper_graph = collect_helper_graph(state, go, helper_env, budget, "prepare-test2json-graph")
    # Explicit pins, then exact package metadata may fetch only ordinary locked
    # module sources from the same proxy. No vulnerability DB or direct fallback.
    pins = [name + "@" + pin[0] for name, pin in sorted(base.PINS.items())]
    raw, _ = command(state, "prepare-pins", [str(go), "mod", "download", "-json", *pins], env, budget, 120)
    downloads = base.stream_json(raw)
    require(len(downloads) == len(base.PINS) and {d.get("Path") for d in downloads} == set(base.PINS), "stage1_pin_set")
    sums = base.sum_entries(base.read_file(state / "work", "go.sum").decode())
    donor = None
    for obj in downloads:
        _, source = base.module_record(obj, sums, state)
        if obj["Path"] == base.BLS:
            donor = source
    require(donor is not None, "prysm_origin_missing")
    for name, digest in base.PRYSM_FILES.items():
        require(base.sha(base.read_file(donor, name)) == digest, "prysm_origin_file_mismatch")
    raw, _ = command(state, "prepare-verify-origin", [str(go), "mod", "verify"], env, budget, 90)
    require(raw.strip() == "all modules verified", "module_verification_output")
    original = tree_manifest(donor, budget.deadline)
    require(FIXTURE not in original["files"], "fixture_already_present")
    fixture = base.read_file(root / MATERIAL, "blackbox_test.go.txt")
    for side in SIDES:
        tree = state / (side + "-prysm")
        copy_tree(donor, tree, original, budget.deadline)
        path = tree / FIXTURE
        path.parent.mkdir()
        path.write_bytes(fixture)
        if side == "candidate":
            patch = str(root / MATERIAL / PATCH)
            command(state, "patch-check", [tool_paths["git"], "apply", "--check", patch], env, budget, 20, cwd=tree)
            command(state, "patch-apply", [tool_paths["git"], "apply", patch], env, budget, 20, cwd=tree)
        derived = tree_manifest(tree, budget.deadline)
        require(set(derived["files"]) == set(original["files"]) | {FIXTURE}, "derived_file_scope")
        changed = {p for p in original["files"] if original["files"][p] != derived["files"][p]}
        require(changed == (set(base.PRYSM_FILES) if side == "candidate" else set()), "derived_production_scope")
        require(derived["files"][FIXTURE] == FIXTURE_SHA, "derived_fixture_identity")
        make_modfile(state / "work", side)
    graphs = {side: collect_graph(state, side, go, env, budget, "prepare") for side in SIDES}
    raw, _ = command(state, "prepare-verify-modules", [str(go), "mod", "verify"], env, budget, 90)
    require(raw.strip() == "all modules verified", "module_verification_output")
    require(tree_manifest(donor, budget.deadline) == original, "donor_modified")
    receipt = {"uid": os.getuid(), "gid": os.getgid(), "go": str(go), "tool_paths": tool_paths,
               "donor": str(donor), "donor_manifest": safe_tree(original), "graphs": graphs, "helper_graph": helper_graph,
               "identity": identity(root, state, tool_paths, budget.deadline)}
    require(receipt["identity"]["tools"] == before_tools, "preparation_tool_changed")
    require(receipt["identity"]["material"] == material, "preparation_material_changed")
    require(receipt["identity"]["copied_inputs"] == base.INPUTS, "copied_input_changed")
    (state / "prepared.json").write_bytes(canonical(receipt))
    checkpoint(state, stage="prepared_metadata_only", graphs=graphs, helper_graph=helper_graph, identity=receipt["identity"],
               donor_manifest=receipt["donor_manifest"], baseline_attempts=[])
    return receipt


LINK_POLICY = "-extldflags=-Wl,-z,noexecstack"


def test_command(converter, binary, baseline):
    pattern = events.BASELINE_PATTERN if baseline else events.CANDIDATE_PATTERN
    return [str(converter), "-t", "-p", events.PACKAGE, str(binary),
            "-test.v=test2json", "-test.count=1", "-test.timeout=50s", "-test.run=" + pattern]


def isolated_command(root, state, receipt):
    env = environment(Path(receipt["go"]), state, True)
    env.update(CC=receipt["tool_paths"]["gcc"], BLS_STAGE1_HOST_NETNS=os.readlink("/proc/self/ns/net"),
               BLS_STAGE1_HOST_PIDNS=os.readlink("/proc/self/ns/pid"))
    tools = receipt["tool_paths"]
    return [tools["sudo"], "-n", tools["timeout"], "--signal=TERM", "--kill-after=10s", "910s",
            tools["unshare"], "--net", "--pid", "--fork", "--mount-proc", "--kill-child=KILL",
            tools["setpriv"], "--reuid=" + str(receipt["uid"]), "--regid=" + str(receipt["gid"]),
            "--clear-groups", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs",
            tools["prlimit"], "--core=0", "--nofile=1024", "--nproc=256", "--fsize=268435456",
            "/usr/bin/env", "-i", *[k + "=" + v for k, v in sorted(env.items())],
            tools["python"], str(root / "scripts/bls_cache_race_ci.py"), "--inner", str(state)]


def inner(root, state):
    receipt = json.loads(base.read_file(state, "prepared.json"))
    go = Path(receipt["go"])
    env = environment(go, state, True)
    env["CC"] = receipt["tool_paths"]["gcc"]
    require(os.getuid() == receipt["uid"] > 0 and os.getgid() == receipt["gid"], "inner_identity")
    require(os.environ.get("GORACE") == events.GORACE and os.environ.get("GOPROXY") == "off", "inner_environment")
    require(os.readlink("/proc/self/ns/pid") != os.environ.get("BLS_STAGE1_HOST_PIDNS"), "pid_namespace_missing")
    budget = process.Budget(INNER_SECONDS, 64 * 1024 * 1024)
    status = {k: v.strip() for k, v in (line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)}
    ip = receipt["tool_paths"]["ip"]
    def ip_json(args, label):
        raw, _ = command(state, label, [ip, "-j", *args], env, budget, 10)
        return json.loads(raw)
    isolation.verify_isolation(status, os.readlink("/proc/self/ns/net"), os.environ.get("BLS_STAGE1_HOST_NETNS"),
                               ip_json(["link"], "inner-links"), ip_json(["-4", "route", "show", "table", "all"], "inner-routes4"),
                               ip_json(["-6", "route", "show", "table", "all"], "inner-routes6"), os.geteuid(), os.getgroups())
    require(identity(root, state, receipt["tool_paths"], budget.deadline) == receipt["identity"], "inner_pre_identity")
    verify_go(state, go, env, budget, "inner-go-env")
    before_graphs = {side: collect_graph(state, side, go, env, budget, "inner-pre") for side in SIDES}
    require(all(graph_fingerprint(before_graphs[s]) == graph_fingerprint(receipt["graphs"][s]) for s in SIDES), "offline_graph_changed")
    checkpoint(state, stage="offline_graphs_verified", graphs=before_graphs)
    binaries, compile_receipts, elf_records = {}, {}, {}
    from elf_stack_policy import inspect_file
    helper_graph = collect_helper_graph(state, go, env, budget, "inner-test2json-graph")
    require(helper_graph == receipt["helper_graph"], "helper_graph_changed")
    converter = state / "test2json"
    _, helper_compile = command(state, "test2json-compile", [str(go), "build", "-mod=readonly",
                                "-ldflags=" + LINK_POLICY, "-o", str(converter), HELPER_PACKAGE], env, budget, 120)
    helper_binary = hash_file(converter)
    helper_info = inspect_file(converter)
    require(helper_info["sha256"] == helper_binary["sha256"] and helper_info["policy_enforced"], "helper_elf_policy")
    helper_tool = {"package": HELPER_PACKAGE, "build_mode": "nonrace_fixed_goroot",
                   "graph": helper_graph, "binary": helper_binary,
                   "elf": {key: helper_info[key] for key in ELF_FIELDS}, "compile_receipt": helper_compile}
    checkpoint(state, helper_tool=helper_tool)
    for side in SIDES:
        binary = state / (side + ".test")
        _, compile_receipts[side] = command(state, side + "-compile", [str(go), "test", "-modfile=" + side + ".mod", "-c", "-race",
                                           "-ldflags=" + LINK_POLICY, "-o", str(binary), events.PACKAGE], env, budget, 300)
        binaries[side] = hash_file(binary)
        info = inspect_file(binary)
        require(info["sha256"] == binaries[side]["sha256"] and info["policy_enforced"], "test_binary_elf_policy")
        elf_records[side] = {key: info[key] for key in ELF_FIELDS}
    checkpoint(state, stage="both_binaries_compiled", compile_receipts=compile_receipts, binary_identity=binaries, elf=elf_records)
    attempts = []
    # Both binaries must be built before either is executed. Only a fully valid
    # quiet pass permits another baseline process; unrelated failure stops here.
    for index in range(1, 4):
        raw, execution = command(state, "baseline-test-" + str(index), test_command(converter, state / "baseline.test", True), env, budget, 60, True)
        result = events.classify(raw, execution["exit_code"], True, str(state / "baseline-prysm"))
        attempts.append({"attempt": index, "execution": execution, "semantic": result})
        checkpoint(state, stage="baseline_executed", baseline_attempts=attempts)
        if result["classification"] == "target_cache_race":
            break
    require(attempts[-1]["semantic"]["classification"] == "target_cache_race", "baseline_not_reproduced")
    raw, execution = command(state, "candidate-tests", test_command(converter, state / "candidate.test", False), env, budget, 60, True)
    candidate = {"execution": execution, "semantic": events.classify(raw, execution["exit_code"], False, str(state / "candidate-prysm"))}
    checkpoint(state, stage="candidate_executed", candidate=candidate)
    after_graphs = {side: collect_graph(state, side, go, env, budget, "inner-post") for side in SIDES}
    require(all(graph_fingerprint(before_graphs[s]) == graph_fingerprint(after_graphs[s]) for s in SIDES), "execution_graph_changed")
    require(identity(root, state, receipt["tool_paths"], budget.deadline) == receipt["identity"], "inner_post_identity")
    require(all(hash_file(state / (s + ".test")) == binaries[s] for s in SIDES), "test_binary_changed")
    require(hash_file(converter) == helper_binary, "helper_binary_changed")
    require(collect_helper_graph(state, go, env, budget, "inner-post-test2json-graph") == helper_graph, "helper_graph_changed")
    require(safe_tree(tree_manifest(Path(receipt["donor"]), budget.deadline)) == receipt["donor_manifest"], "donor_modified")
    result = {"schema": "bls-stage1-private-comparison-v1", "status": "target_red_and_six_candidate_passes",
              "prior_stage0_sha256": STAGE0_SHA, "not_production_acceptance": True, "license_approval": False,
              "native_include_closure_proven": False, "graphs": before_graphs,
              "post_graph_sha256": {s: base.sha(canonical(after_graphs[s])) for s in SIDES},
              "identity": receipt["identity"], "donor_manifest": receipt["donor_manifest"],
              "binary_identity_before_and_after": binaries, "compile_receipts": compile_receipts, "elf": elf_records,
              "baseline_attempts": attempts, "candidate": candidate, "helper_tool": helper_tool,
              "isolation": {"nonroot": True, "zero_capabilities": True, "no_new_privileges": True,
                            "network_and_pid_namespace_replaced": True, "down_loopback_only": True, "routes_empty": True},
              "policy": {"go": base.GO_VERSION, "gorace": events.GORACE, "link_policy": LINK_POLICY,
                         "baseline_tests": [events.COLD], "candidate_tests": list(events.TESTS), "max_baseline_processes": 3,
                         "prepare_seconds": PREPARE_SECONDS, "inner_seconds": INNER_SECONDS, "memory_hard_limit": False,
                         "performance_or_rss_acceptance": False, "complete_toolchain_attestation": False}}
    require(len(canonical(result)) <= MAX_REPORT_BYTES, "stage1_report_limit")
    (state / "result.json").write_bytes(canonical(result))


ELF_FIELDS = ("elf_class", "byte_order", "machine", "elf_type", "program_header_count", "gnu_stack_flags", "load_flags")
COMMAND_LABELS = {"prepare-go-env", "prepare-pins", "prepare-verify-origin", "prepare-verify-modules",
                  "patch-check", "patch-apply", "inner-links", "inner-routes4", "inner-routes6", "inner-go-env",
                  "outer-namespace", "candidate-tests", "prepare-test2json-graph", "inner-test2json-graph",
                  "inner-post-test2json-graph", "test2json-compile"} | {
    phase + "-" + side + "-" + kind for phase in ("prepare", "inner-pre", "inner-post")
    for side in SIDES for kind in ("graph", "modules")} | {
    side + "-compile" for side in SIDES} | {"baseline-test-" + str(n) for n in range(1, 4)}
# JSON and exact textual metadata consumers must never see merged stderr.
METADATA_LABELS = {"prepare-go-env", "inner-go-env", "inner-links", "inner-routes4", "inner-routes6",
                   "prepare-pins", "prepare-verify-origin",
                   "prepare-verify-modules", "prepare-test2json-graph", "inner-test2json-graph",
                   "inner-post-test2json-graph"} | {
    phase + "-" + side + "-" + kind for phase in ("prepare", "inner-pre", "inner-post")
    for side in SIDES for kind in ("graph", "modules")}

PROGRESS_KEYS = {"schema", "status", "license_approval", "native_include_closure_proven", "stage", "graphs", "identity",
                 "donor_manifest", "baseline_attempts", "compile_receipts", "binary_identity", "elf", "candidate",
                 "current_command", "last_command", "command_failure", "current_tool", "helper_graph", "helper_tool"}


def exact_keys(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys), "export_shape")


def digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "export_digest")
    return value


def integer(value, minimum, maximum):
    require(type(value) is int and minimum <= value <= maximum, "export_integer")
    return value


def export_stream(value):
    exact_keys(value, ("bytes", "sha256"))
    integer(value["bytes"], 0, process.COMMAND_BYTES)
    digest(value["sha256"])
    return dict(value)


def export_receipt(value, separate=False):
    if separate:
        exact_keys(value, ("exit_code", "capture_mode", "stdout_private", "stderr_private", "elapsed_milliseconds"))
        require(value["capture_mode"] == metadata_capture.MODE, "export_capture_mode")
        stdout, stderr = export_stream(value["stdout_private"]), export_stream(value["stderr_private"])
        require(stdout["bytes"] + stderr["bytes"] <= process.COMMAND_BYTES, "export_stream_total")
        result = {"capture_mode": metadata_capture.MODE, "stdout_private": stdout, "stderr_private": stderr}
    else:
        exact_keys(value, ("exit_code", "raw_private_bytes", "raw_private_sha256", "elapsed_milliseconds"))
        integer(value["raw_private_bytes"], 0, process.COMMAND_BYTES)
        digest(value["raw_private_sha256"])
        result = {"raw_private_bytes": value["raw_private_bytes"], "raw_private_sha256": value["raw_private_sha256"]}
    result["exit_code"] = integer(value["exit_code"], -255, 255)
    result["elapsed_milliseconds"] = integer(value["elapsed_milliseconds"], 0, 1000000)
    return result


def export_semantic(value, baseline):
    require(isinstance(value, dict), "export_semantic")
    red = value.get("classification") == "target_cache_race"
    blocks = integer(value.get("target_race_blocks"), 1 if red else 0, 16384 if red else 0)
    require(baseline or not red, "export_candidate_race")
    expected = {"classification": "target_cache_race" if red else "quiet_pass",
                "outcomes": {n: "fail" if red else "pass" for n in ((events.COLD,) if baseline else events.TESTS)},
                "package_outcome": "fail" if red else "pass", "exit_code": 1 if red else 0,
                "target_race_blocks": blocks, "frame_classes": [{
                    "read": "nonblocking_Get_via_public_key_from_bytes", "write": "nonblocking_Add_via_public_key_from_bytes"
                } for _ in range(blocks)], "coverage": "cold_first_use_only" if baseline else "six_blackbox_tests"}
    require(value == expected, "export_semantic")
    return expected


def export_test(value, baseline):
    exact_keys(value, ("execution", "semantic"))
    result = {"execution": export_receipt(value["execution"]), "semantic": export_semantic(value["semantic"], baseline)}
    require(result["execution"]["exit_code"] == result["semantic"]["exit_code"], "export_test_exit")
    return result


def export_attempts(value, complete=False):
    require(isinstance(value, list) and 0 <= len(value) <= 3, "export_attempts")
    result = []
    for index, attempt in enumerate(value, 1):
        exact_keys(attempt, ("attempt", "execution", "semantic"))
        require(type(attempt["attempt"]) is int and attempt["attempt"] == index, "export_attempt_index")
        item = export_test({k: attempt[k] for k in ("execution", "semantic")}, True)
        require(index == len(value) or item["semantic"]["classification"] == "quiet_pass", "export_attempt_order")
        result.append({"attempt": index, **item})
    if complete:
        require(result and result[-1]["semantic"]["classification"] == "target_cache_race", "export_missing_red")
    return result


def export_binaries(value):
    exact_keys(value, SIDES)
    for item in value.values():
        exact_keys(item, ("sha256", "bytes"))
        digest(item["sha256"])
        integer(item["bytes"], 1, MAX_BINARY_BYTES)
    return value


def export_elf(value):
    exact_keys(value, SIDES)
    for item in value.values():
        exact_keys(item, ELF_FIELDS)
        require(item["elf_class"] == 64 and item["byte_order"] == "little" and item["machine"] == 62 and item["elf_type"] in (2, 3), "export_elf_identity")
        integer(item["program_header_count"], 1, 128)
        integer(item["gnu_stack_flags"], 0, 7)
        require(not item["gnu_stack_flags"] & 1 and isinstance(item["load_flags"], list) and 1 <= len(item["load_flags"]) <= 128, "export_elf_stack")
        for flags in item["load_flags"]:
            integer(flags, 0, 7)
            require(flags & 3 != 3, "export_elf_load")
    return value


def export_graphs(value, receipt):
    exact_keys(value, SIDES)
    require(receipt is not None, "export_graph_without_origin")
    result = json.loads(json.dumps(receipt["graphs"]))
    for side in SIDES:
        require(graph_fingerprint(value[side]) == graph_fingerprint(result[side]), "export_graph_mismatch")
        for old, new in zip(result[side]["packages"], value[side]["packages"]):
            require(("generated" in old) == ("generated" in new), "export_generated_presence")
            if "generated" in new:
                item = new["generated"]
                exact_keys(item, ("logical_name", "materialized", "sha256"))
                require(new["id"] == events.PACKAGE + ".test" and item["logical_name"] == "_testmain.go" and type(item["materialized"]) is bool, "export_generated")
                if item["materialized"]:
                    digest(item["sha256"])
                else:
                    require(item["sha256"] is None, "export_generated")
                old["generated"] = dict(item)
    return result


def export_helper(value, receipt):
    exact_keys(value, ("package", "build_mode", "graph", "binary", "elf", "compile_receipt"))
    require(value["package"] == HELPER_PACKAGE and value["build_mode"] == "nonrace_fixed_goroot", "export_helper_identity")
    require(receipt is not None and value["graph"] == receipt["helper_graph"], "export_helper_graph")
    # Reuse the exact existing scalar validators, with no path-bearing records.
    binary = export_binaries({side: value["binary"] for side in SIDES})["baseline"]
    elf = export_elf({side: value["elf"] for side in SIDES})["baseline"]
    compilation = export_receipt(value["compile_receipt"])
    require(compilation["exit_code"] == 0, "export_helper_compile")
    return {"package": HELPER_PACKAGE, "build_mode": "nonrace_fixed_goroot", "graph": receipt["helper_graph"],
            "binary": binary, "elf": elf, "compile_receipt": compilation}


def export_progress(value, receipt):
    require(isinstance(value, dict) and set(value) <= PROGRESS_KEYS, "export_progress_shape")
    require(value.get("schema") == "bls-stage1-progress-v1" and value.get("status") == "incomplete_not_acceptance" and
            value.get("license_approval") is False and value.get("native_include_closure_proven") is False, "export_progress_header")
    result = {key: value[key] for key in ("schema", "status", "license_approval", "native_include_closure_proven")}
    for key in ("identity", "donor_manifest"):
        if key in value:
            require(receipt is not None and value[key] == receipt[key], "export_origin_mismatch")
            result[key] = receipt[key]
    if "current_tool" in value:
        require(value["current_tool"] in TOOL_LABELS, "export_tool_label")
        result["current_tool"] = value["current_tool"]
    if "helper_graph" in value:
        require(receipt is not None and value["helper_graph"] == receipt["helper_graph"], "export_helper_graph")
        result["helper_graph"] = receipt["helper_graph"]
    if "helper_tool" in value:
        result["helper_tool"] = export_helper(value["helper_tool"], receipt)
    if "graphs" in value:
        result["graphs"] = export_graphs(value["graphs"], receipt)
    if "stage" in value:
        require(value["stage"] in {"prepared_metadata_only", "offline_graphs_verified", "both_binaries_compiled", "baseline_executed", "candidate_executed"}, "export_stage")
        result["stage"] = value["stage"]
    if "current_command" in value:
        require(value["current_command"] in COMMAND_LABELS, "export_command_label")
        result["current_command"] = value["current_command"]
    if "last_command" in value:
        item = dict(value["last_command"])
        label = item.pop("label", None)
        require(label in COMMAND_LABELS, "export_command_label")
        result["last_command"] = {"label": label, **export_receipt(item, label in METADATA_LABELS)}
    if "command_failure" in value:
        item = value["command_failure"]
        require(isinstance(item, dict) and item.get("label") in COMMAND_LABELS, "export_command_failure")
        if item["label"] in METADATA_LABELS:
            exact_keys(item, ("label", "reason", "capture_mode", "stdout_private", "stderr_private"))
            require(item["capture_mode"] == metadata_capture.MODE and item["reason"] in metadata_capture.REASONS, "export_command_failure")
            stdout, stderr = export_stream(item["stdout_private"]), export_stream(item["stderr_private"])
            require(stdout["bytes"] + stderr["bytes"] <= process.COMMAND_BYTES, "export_stream_total")
            result["command_failure"] = {"label": item["label"], "reason": item["reason"], "capture_mode": metadata_capture.MODE,
                                         "stdout_private": stdout, "stderr_private": stderr}
        else:
            exact_keys(item, ("label", "reason", "raw_private_bytes", "raw_private_sha256"))
            require(item["reason"] in (None, "timeout", "output_limit", "termination_permission_error"), "export_command_failure")
            integer(item["raw_private_bytes"], 0, process.COMMAND_BYTES)
            digest(item["raw_private_sha256"])
            result["command_failure"] = dict(item)
    if "baseline_attempts" in value:
        result["baseline_attempts"] = export_attempts(value["baseline_attempts"])
    if "candidate" in value:
        result["candidate"] = export_test(value["candidate"], False)
    if "compile_receipts" in value:
        exact_keys(value["compile_receipts"], SIDES)
        result["compile_receipts"] = {s: export_receipt(value["compile_receipts"][s]) for s in SIDES}
        require(all(x["exit_code"] == 0 for x in result["compile_receipts"].values()), "export_compile_failed")
    if "binary_identity" in value:
        result["binary_identity"] = export_binaries(value["binary_identity"])
    if "elf" in value:
        result["elf"] = export_elf(value["elf"])
    return result


def export_success(value, receipt):
    keys = {"schema", "status", "prior_stage0_sha256", "not_production_acceptance", "license_approval",
            "native_include_closure_proven", "graphs", "post_graph_sha256", "identity", "donor_manifest",
            "binary_identity_before_and_after", "compile_receipts", "elf", "baseline_attempts", "candidate", "isolation", "policy", "helper_tool"}
    exact_keys(value, keys)
    require(value["schema"] == "bls-stage1-private-comparison-v1" and value["status"] == "target_red_and_six_candidate_passes" and
            value["prior_stage0_sha256"] == STAGE0_SHA and value["not_production_acceptance"] is True and
            value["license_approval"] is False and value["native_include_closure_proven"] is False, "export_success_header")
    expected_policy = {"go": base.GO_VERSION, "gorace": events.GORACE, "link_policy": LINK_POLICY,
                       "baseline_tests": [events.COLD], "candidate_tests": list(events.TESTS), "max_baseline_processes": 3,
                       "prepare_seconds": PREPARE_SECONDS, "inner_seconds": INNER_SECONDS, "memory_hard_limit": False,
                       "performance_or_rss_acceptance": False, "complete_toolchain_attestation": False}
    require(value["policy"] == expected_policy, "export_policy")
    require(value["isolation"] == {"nonroot": True, "zero_capabilities": True, "no_new_privileges": True,
            "network_and_pid_namespace_replaced": True, "down_loopback_only": True, "routes_empty": True}, "export_isolation")
    progress = {"schema": "bls-stage1-progress-v1", "status": "incomplete_not_acceptance", "license_approval": False,
                "native_include_closure_proven": False, **{k: value[k] for k in (
                    "graphs", "identity", "donor_manifest", "compile_receipts", "elf", "baseline_attempts", "candidate", "helper_tool")},
                "binary_identity": value["binary_identity_before_and_after"]}
    checked = export_progress(progress, receipt)
    export_attempts(checked["baseline_attempts"], complete=True)
    exact_keys(value["post_graph_sha256"], SIDES)
    for digest_value in value["post_graph_sha256"].values():
        digest(digest_value)
    result = dict(value)
    result.update({k: checked[k] for k in ("graphs", "identity", "donor_manifest", "compile_receipts", "elf", "baseline_attempts", "candidate", "helper_tool")})
    result["binary_identity_before_and_after"] = checked["binary_identity"]
    result["policy"] = expected_policy
    return result


def checkpoint(state, **updates):
    path = state / "progress.json"
    value = json.loads(base.read_file(state, path.name)) if path.exists() else {
        "schema": "bls-stage1-progress-v1", "status": "incomplete_not_acceptance",
        "license_approval": False, "native_include_closure_proven": False}
    require(set(value) <= PROGRESS_KEYS and set(updates) <= PROGRESS_KEYS, "checkpoint_unknown_field")
    value.update(updates)
    require(len(canonical(value)) <= MAX_REPORT_BYTES, "progress_limit")
    path.write_bytes(canonical(value))


SAFE_ERROR_CODES = frozenset({
    'baseline_not_reproduced',
    'cache_consumer_graph_mismatch',
    'capture_reason',
    'capture_status_identity',
    'checkpoint_unknown_field',
    'command_label',
    'comparison_or_preparation_failed',
    'copied_input_changed',
    'deadline',
    'declared_pin_mismatch',
    'derived_file_scope',
    'derived_fixture_identity',
    'derived_production_scope',
    'derived_source_exists',
    'donor_changed_during_copy',
    'donor_modified',
    'download_checksum_mismatch',
    'download_failed',
    'download_identity_mismatch',
    'duplicate_go_sum',
    'duplicate_or_overlapping_test_run',
    'duplicate_package_instance',
    'duplicate_package_start',
    'duplicate_race_package',
    'duplicate_test_json_key',
    'effective_declared_pin_mismatch',
    'effective_dir_mismatch',
    'effective_modsum_mismatch',
    'effective_sum_mismatch',
    'empty_json',
    'empty_source_tree',
    'execution_graph_changed',
    'existing_prysm_replacement',
    'export_attempt_index',
    'export_attempt_order',
    'export_attempts',
    'export_candidate_race',
    'export_capture_mode',
    'export_command_failure',
    'export_command_label',
    'export_compile_failed',
    'export_digest',
    'export_elf_identity',
    'export_elf_load',
    'export_elf_stack',
    'export_generated',
    'export_generated_presence',
    'export_graph_mismatch',
    'export_graph_without_origin',
    'export_helper_compile',
    'export_helper_graph',
    'export_helper_identity',
    'export_integer',
    'export_isolation',
    'export_missing_red',
    'export_origin_mismatch',
    'export_policy',
    'export_progress_header',
    'export_progress_shape',
    'export_semantic',
    'export_shape',
    'export_stage',
    'export_status',
    'export_stream_total',
    'export_success_header',
    'export_test_exit',
    'export_tool_label',
    'external_build_configuration',
    'external_target',
    'external_toolchain',
    'failure_shape',
    'file_escape',
    'fixture_already_present',
    'generated_source_escape',
    'go_binary_changed',
    'go_environment_mismatch',
    'go_environment_shape',
    'go_metadata_failed',
    'go_root_mismatch',
    'go_root_version_changed',
    'go_root_version_mismatch',
    'helper_binary_changed',
    'helper_command_missing',
    'helper_duplicate_package',
    'helper_elf_policy',
    'helper_graph_changed',
    'helper_not_command',
    'helper_package_origin',
    'helper_requires_offline_resolution',
    'helper_source_escape',
    'helper_source_limit',
    'helper_sources_missing',
    'helper_unresolved_import',
    'identity_file_limit',
    'identity_file_missing',
    'incomplete_tests',
    'inner_environment',
    'inner_identity',
    'inner_post_identity',
    'inner_pre_identity',
    'inner_state_path',
    'input_hash_mismatch',
    'invalid_baseline_mode',
    'invalid_effective_module',
    'invalid_go_sum',
    'invalid_import_id',
    'invalid_import_metadata',
    'invalid_json',
    'invalid_module_name',
    'invalid_module_version',
    'invalid_relative_name',
    'invalid_replacement',
    'invalid_replacement_version',
    'invalid_source_root',
    'invalid_test_json',
    'invalid_testmain',
    'invalid_utf8',
    'json_limit',
    'json_object_required',
    'license_input_mismatch',
    'local_prysm_declared_identity',
    'local_prysm_effective_identity',
    'local_prysm_false_checksum',
    'material_hash_mismatch',
    'material_manifest_scope',
    'material_pin_mismatch',
    'material_scope',
    'material_test_scope',
    'metadata_budget_exhausted',
    'metadata_capture_failed',
    'metadata_label',
    'metadata_log_exists',
    'metadata_status_code',
    'metadata_status_shape',
    'metadata_stream_identity',
    'metadata_stream_limit',
    'metadata_stream_shape',
    'metadata_success_status',
    'metadata_stdout_identity',
    'missing_baseline_bls',
    'missing_go',
    'missing_or_symlink_file',
    'missing_package_sources',
    'missing_platform_tool',
    'missing_race_modules',
    'missing_test_graph_instances',
    'module_dir_escape',
    'module_error',
    'module_inventory_mismatch',
    'module_modsum_mismatch',
    'module_sum_mismatch',
    'module_verification_output',
    'nonfinite_test_json_number',
    'object_limit',
    'offline_graph_changed',
    'ordinary_linux_user_required',
    'original_effective_dir_mismatch',
    'outer_post_identity',
    'oversized_file',
    'package_dir_escape',
    'package_effective_dir_mismatch',
    'package_effective_sum_mismatch',
    'package_error',
    'package_event_rejected',
    'package_module_dir_mismatch',
    'package_module_identity_mismatch',
    'package_original_checksum',
    'package_output_ownership',
    'package_replacement_identity_mismatch',
    'pid_namespace_missing',
    'pin_mismatch',
    'preparation_material_changed',
    'preparation_tool_changed',
    'probe_import_mismatch',
    'progress_limit',
    'prysm_origin_file_mismatch',
    'prysm_origin_missing',
    'race_access_count',
    'race_access_frame_mismatch',
    'race_access_identity',
    'race_access_kinds',
    'race_access_order',
    'race_block_header',
    'race_fixture_files',
    'race_generated_escape',
    'race_generated_missing',
    'race_generated_name',
    'race_import_unresolved',
    'race_marker_mismatch',
    'race_module_inventory',
    'race_package_dir_escape',
    'race_package_error',
    'race_package_sources_missing',
    'race_source_file_limit',
    'race_target_chain_missing',
    'race_test_instances_missing',
    'race_testmain_shape',
    'race_unknown_module',
    'report_limit',
    'source_byte_limit',
    'source_copy_mismatch',
    'source_deadline',
    'source_directory_symlink',
    'source_file_limit',
    'source_inputs_changed',
    'source_root_invalid',
    'source_tree_limit',
    'stage1_comparison_failed',
    'stage1_go_environment',
    'stage1_goroot_version',
    'stage1_pin_set',
    'stage1_report_limit',
    'stage1_result_missing',
    'temporary_lock_or_compat_changed',
    'temporary_probe_changed',
    'test_action_rejected',
    'test_binary_changed',
    'test_binary_elf_policy',
    'test_elapsed_shape',
    'test_event_count',
    'test_event_shape',
    'test_exit_rejected',
    'test_outcome_mismatch',
    'test_output_encoding',
    'test_output_field',
    'test_output_limit',
    'test_output_outside_run',
    'test_output_ownership',
    'test_output_shape',
    'test_output_type',
    'test_package_or_terminal',
    'test_runtime_failure',
    'test_terminal_rejected',
    'test_text_outcomes_mismatch',
    'test_text_package_mismatch',
    'test_text_runs_mismatch',
    'test_time_shape',
    'tool_identity_labels',
    'tool_output_limit',
    'truncated_or_unframed_race',
    'unexpected_bls_wrapper',
    'unexpected_main_module',
    'unexpected_main_package',
    'unexpected_module_replacement',
    'unexpected_original_checksum',
    'unexpected_race',
    'unexpected_race_marker',
    'unexpected_relay_package',
    'unexpected_test',
    'unexpected_test_instance',
    'unexpected_testmain_sources',
    'unknown_argument',
    'unknown_compat_input',
    'unknown_generated_source',
    'unknown_missing_generated_source',
    'unknown_package_module',
    'unknown_side',
    'unknown_stage1_argument',
    'unrecognized_test_output',
    'unresolved_import_instance',
    'unsafe_generated_source',
    'unsafe_import_id',
    'unsafe_progress_rejected',
    'unsafe_relative_name',
    'upstream_source_mismatch',
    'upstream_tests_selected',
    'workspace_enabled',
    'workspace_file',
})

def safe_failure(error):
    if isinstance(error, base.GateError) and (str(error) in SAFE_ERROR_CODES or
            any(str(error) == prefix + label for label in COMMAND_LABELS for prefix in ("command_failed_", "command_capture_failed_"))):
        return str(error)
    return "comparison_or_preparation_failed"


def emit_safe(payload, sink=None):
    """Emit only an already-projected payload with its exact printed-byte receipt."""
    if sink is None:
        sink = sys.stdout
    require(payload.get("status") in {"incomplete_not_acceptance", "target_red_and_six_candidate_passes"}, "export_status")
    data = (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()
    require(len(data) <= MAX_REPORT_BYTES, "stage1_report_limit")
    receipt = {"bytes": len(data), "sha256": base.sha(data), "status": payload["status"]}
    sink.write("BLS_STAGE1_SAFE_RECEIPT " + json.dumps(receipt, sort_keys=True) + "\n")
    sink.write(data.decode())
    sink.flush()
    return receipt


def run(root):
    value = shutil.which("go")
    require(value is not None, "missing_go")
    go = Path(value).resolve()
    failure, payload, receipt = None, None, None
    with tempfile.TemporaryDirectory(prefix="bls-stage1-") as temporary:
        state = Path(temporary).resolve()
        try:
            receipt = prepare(root, state, go)
            budget = process.Budget(930, 64 * 1024 * 1024)
            command(state, "outer-namespace", isolated_command(root, state, receipt), environment(go, state, True), budget, 925)
            raw = base.read_file(state, "result.json")
            require(len(raw) <= MAX_REPORT_BYTES, "stage1_report_limit")
            payload = json.loads(raw)
            require(payload.get("schema") == "bls-stage1-private-comparison-v1" and
                    payload.get("status") == "target_red_and_six_candidate_passes", "stage1_result_missing")
            require(identity(root, state, receipt["tool_paths"], budget.deadline) == receipt["identity"], "outer_post_identity")
            payload = export_success(payload, receipt)
        except Exception as error:
            failure = safe_failure(error)
            try:
                if (state / "failure.json").exists():
                    detail = json.loads(base.read_file(state, "failure.json"))
                    code = detail.get("gate_code")
                    require(set(detail) == {"gate_code"} and isinstance(code, str), "failure_shape")
                    failure = safe_failure(base.GateError(code))
                progress = json.loads(base.read_file(state, "progress.json")) if (state / "progress.json").exists() else None
                progress = export_progress(progress, receipt) if progress is not None else None
            except Exception:
                failure, progress = "unsafe_progress_rejected", None
            payload = {"schema": "bls-stage1-failure-v1", "status": "incomplete_not_acceptance",
                       "gate_code": failure, "progress": progress}
    # Cleanup completes before any receipt is exported. Raw tool output/source/
    # binary/cache is never an artifact or stdout payload, even after failure.
    emit_safe(payload)
    if failure is not None:
        error = base.GateError("stage1_comparison_failed")
        error.evidence_emitted = True
        raise error


def main():
    root = Path(__file__).resolve().parents[1]
    inner_state = None
    try:
        if len(sys.argv) == 1:
            run(root)
        else:
            require(len(sys.argv) == 3 and sys.argv[1] == "--inner", "unknown_stage1_argument")
            state = Path(sys.argv[2])
            require(state.is_absolute() and state.is_dir() and not state.is_symlink() and
                    state.resolve() == state and state.stat().st_uid == os.getuid() and
                    not state.is_relative_to(root) and not root.is_relative_to(state), "inner_state_path")
            inner_state = state
            inner(root, state)
        return 0
    except Exception as error:
        code = safe_failure(error)
        if inner_state is not None:
            (inner_state / "failure.json").write_bytes(canonical({"gate_code": code}))
        elif not getattr(error, "evidence_emitted", False):
            emit_safe({"schema": "bls-stage1-failure-v1", "status": "incomplete_not_acceptance",
                       "gate_code": code, "progress": None})
        print("BLS_STAGE1_ERROR:" + code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
