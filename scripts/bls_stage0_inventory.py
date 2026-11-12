#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixed-input, metadata-only BLS inventory. Never compiles or runs tests."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time

MAIN = "github.com/keienWang/bsc-p2p-relay"
PROBE = MAIN + "/internal/blsgraphprobe"
BLS = "github.com/prysmaticlabs/prysm/v5"
BLST = "github.com/supranational/blst"
LRU = "github.com/hashicorp/golang-lru"
GO_VERSION = "go1.26.11"
PROBE_FILE = "research/bls-stage0/probe_test.go.txt"
INPUTS = {
    "LICENSE": "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986",
    "go.mod": "c64e1050ac55b5e6fe3c05677038d3a61a7f552b3215685cd9e808947b5f6f43",
    "go.sum": "ff601aaf5f3ed0c3caa7a4b8eb7a58901c34cdcd4c9c22e3e8e9bf2ad2cbbfbd",
    "compat/stunv2/LICENSE": "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986",
    "compat/stunv2/README.md": "7e64c297fd853812c85afffbd1ce9d7610d978c73bb2f7edf97e09897d5dc582",
    "compat/stunv2/UPSTREAM_LICENSE.txt": "6483daf6c8aa2c8192528e60be098aa82dd18f6d7b96dc8246f4f7c3333fbf3f",
    "compat/stunv2/go.mod": "223cdbfe991d1b6b098aeeadb49e4b9a43192d710d5f196b478a3f212ff02f2e",
    "compat/stunv2/go.sum": "8956408fd295e7a57192a560acb2bb0365d40009244a7ebed407e408d8b87556",
    "compat/stunv2/stun.go": "69250f3a3af80411478ab4d0a9d771e7df3cc3ad90915c4276e135c6427b2e02",
}
PINS = {
    BLS: ("v5.3.2", "h1:yV44gm5DENWG2l17JjjaWMCagJkwSZz5ZG6zlVjyQgg=",
          "h1:2SaUMpJ+O8r/pcnNDMHbrk0Ki9ObQXvfRc+rQHovzVk="),
    BLST: ("v0.3.17-0.20260401165437-a3c15d6a6c68", "h1:dKUVoIwHxicmwbka/QV/C4Nbq6wPPKdT5TEiTb+8RSc=",
           "h1:jZJtfjgudtNl4en1tzwPIV3KjUnQUvG3/j+w+fVonLw="),
    LRU: ("v0.5.5-0.20210104140557-80c98217689d", "h1:dg1dEPuWpEqDnvIw251EVy4zlP8gWbsGj4BsUKCRpYs=",
          "h1:iADmTwqILo4mZ8BN3D2Q6+9jd8WM5uGBxy+E8yxSoD4="),
}
PRYSM_FILES = {
    "crypto/bls/blst/init.go": "c12e0b97d011a408e1522543b744a0393b392e00603f6e49799014e31382945c",
    "crypto/bls/blst/public_key.go": "0b7acd3b01149637cf0501df26732bb7c0be0e62388f96da1bcd837eac6bba12",
}
FILE_FIELDS = ("GoFiles", "CgoFiles", "CFiles", "CXXFiles", "MFiles", "HFiles",
               "FFiles", "SFiles", "SwigFiles", "SwigCXXFiles", "SysoFiles", "EmbedFiles")
ENV_KEYS = ("GOVERSION", "GOOS", "GOARCH", "GOAMD64", "GOHOSTOS", "GOHOSTARCH", "CGO_ENABLED",
            "GOMOD", "GOWORK", "GOFLAGS", "GOPROXY", "GOSUMDB", "GOTOOLCHAIN",
            "GOROOT", "GOMODCACHE", "GOCACHE")
FLAGS = "-mod=readonly -buildvcs=false -p=1"
MAX_RAW = 16 * 1024 * 1024
MAX_REPORT = 4 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_BYTES = 128 * 1024 * 1024
TOTAL_SECONDS = 600
COMMAND_SECONDS = 240
PROBE_SHA = "796dfdaa9cfae4a5adea8370ef131377de20a3d6268a7f0e193463704d0268b0"


class GateError(Exception):
    """Stable error codes only. Do not include paths, source or tool stderr."""


def require(ok, code):
    if not ok:
        raise GateError(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def relative(value):
    require(isinstance(value, str) and value != "", "invalid_relative_name")
    p = PurePosixPath(value)
    require(not p.is_absolute() and "\\" not in value and
            all(part not in ("", ".", "..") for part in value.split("/")) and
            re.fullmatch(r"[A-Za-z0-9_.+@!/-]+", value), "unsafe_relative_name")
    return p


def read_file(root, name):
    p = root / relative(name)
    require(p.is_file() and not p.is_symlink(), "missing_or_symlink_file")
    require(p.resolve().is_relative_to(root.resolve()), "file_escape")
    require(p.stat().st_size <= MAX_FILE_BYTES, "oversized_file")
    with p.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    require(len(data) <= MAX_FILE_BYTES, "oversized_file")
    return data


def identifier(value):
    require(isinstance(value, str), "invalid_import_id")
    if " [" in value:
        require(value == PROBE + " [" + PROBE + ".test]", "unexpected_test_instance")
    else:
        require(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./!+-]*", value) and
                not value.startswith("/") and ".." not in value.split("/"),
                "unsafe_import_id")
    return value


def module_name(value):
    identifier(value)
    require(" [" not in value, "invalid_module_name")
    return value


def stream_json(text):
    require(isinstance(text, str) and len(text.encode()) <= MAX_RAW, "json_limit")
    decoder = json.JSONDecoder()
    result = []
    while text.strip():
        try:
            obj, end = decoder.raw_decode(text.lstrip())
        except (ValueError, TypeError):
            raise GateError("invalid_json") from None
        require(isinstance(obj, dict), "json_object_required")
        result.append(obj)
        require(len(result) <= 512, "object_limit")
        text = text.lstrip()[end:]
    require(result, "empty_json")
    return result


def stop_group(proc):
    """Bounded reap attempt; disposable CI runner is the final cleanup boundary."""
    for sig, seconds in ((signal.SIGTERM, 0.5), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            continue
    return proc.poll() is not None


def capture(command, cwd, env, deadline):
    """Bound streams and waits without Popen's unbounded context-manager exit."""
    deadline = min(deadline, time.monotonic() + COMMAND_SECONDS)
    require(deadline > time.monotonic(), "deadline")
    proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    output = bytearray()
    total = 0
    digests = {key: hashlib.sha256() for key in ("stdout", "stderr")}
    lengths = {key: 0 for key in digests}
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, "stderr")
    try:
        while sel.get_map():
            remaining = deadline - time.monotonic()
            require(remaining > 0, "deadline")
            for key, _ in sel.select(min(remaining, 1)):
                part = os.read(key.fileobj.fileno(), 65536)
                if not part:
                    sel.unregister(key.fileobj)
                    continue
                total += len(part)
                digests[key.data].update(part)
                lengths[key.data] += len(part)
                require(total <= MAX_RAW, "tool_output_limit")
                if key.data == "stdout":
                    output.extend(part)
        code = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        require(code == 0, "go_metadata_failed")
    except BaseException as error:
        complete = stop_group(proc)
        if isinstance(error, GateError):
            error.safe_diagnostics = {"tool_" + key + "_sha256": value.hexdigest()
                                      for key, value in digests.items()}
            error.safe_diagnostics.update({"tool_" + key + "_bytes": value
                                           for key, value in lengths.items()})
            error.safe_diagnostics["leader_reaped"] = complete
        raise
    finally:
        sel.close()
        proc.stdout.close()
        proc.stderr.close()
    try:
        return output.decode("utf-8", errors="strict")
    except UnicodeError:
        raise GateError("invalid_utf8") from None


def reject_environment(environ):
    forbidden = ("GOFLAGS", "GOEXPERIMENT", "GO111MODULE", "GOPROXY", "GOSUMDB",
                 "GOPRIVATE", "GONOPROXY", "GONOSUMDB", "GOINSECURE", "GOAUTH",
                 "CGO_CFLAGS", "CGO_CPPFLAGS", "CGO_CXXFLAGS", "CGO_FFLAGS",
                 "CGO_LDFLAGS", "CC", "CXX", "CPATH", "LIBRARY_PATH")
    require(not any(environ.get(k) for k in forbidden), "external_build_configuration")
    require(environ.get("GOWORK", "off") == "off", "workspace_enabled")
    require(environ.get("GOTOOLCHAIN", "local") == "local", "external_toolchain")
    for key, value in (("GOOS", "linux"), ("GOARCH", "amd64"), ("GOAMD64", "v1"), ("CGO_ENABLED", "1")):
        require(not environ.get(key) or environ[key] == value, "external_target")


def clean_env(go, state):
    return {
        "PATH": str(go.parent) + ":/usr/bin:/bin", "HOME": str(state / "home"),
        "TMPDIR": str(state / "tmp"), "GOCACHE": str(state / "buildcache"),
        "GOMODCACHE": str(state / "modcache"), "GOTOOLCHAIN": "local",
        "GOWORK": "off", "GOENV": "off", "GOTELEMETRY": "off", "GOAUTH": "off",
        "GOFLAGS": FLAGS, "GOOS": "linux", "GOARCH": "amd64", "GOAMD64": "v1",
        "CGO_ENABLED": "1", "GOMAXPROCS": "2", "GOPROXY": "https://proxy.golang.org",
        "GOSUMDB": "off", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
    }


def input_hashes(root):
    expected = dict(INPUTS)
    expected[PROBE_FILE] = PROBE_SHA
    result = {}
    for name, digest in expected.items():
        result[name] = sha(read_file(root, name))
        require(result[name] == digest, "input_hash_mismatch")
    actual = {str(p.relative_to(root)) for p in (root / "compat/stunv2").rglob("*") if p.is_file()}
    require(actual == {p for p in INPUTS if p.startswith("compat/")}, "unknown_compat_input")
    require(not (root / "go.work").exists(), "workspace_file")
    return result


def sum_entries(text):
    result = {}
    for line in text.splitlines():
        fields = line.split()
        require(len(fields) == 3, "invalid_go_sum")
        key = tuple(fields[:2])
        require(key not in result, "duplicate_go_sum")
        result[key] = fields[2]
    return result


def module_record(obj, sums, state):
    require(not obj.get("Error"), "module_error")
    path = module_name(obj.get("Path"))
    version = obj.get("Version")
    require(isinstance(version, str) and re.fullmatch(r"v[A-Za-z0-9_.+-]+", version), "invalid_module_version")
    # Replacement applicability is not known until the real graph is collected.
    # This initial collector stops for review rather than silently following it.
    require(not obj.get("Main"), "unexpected_main_module")
    if obj.get("Replace"):
        replacement = obj["Replace"]
        require(isinstance(replacement, dict), "invalid_replacement")
        effective = {"kind": "local_path", "path_redacted": True}
        if replacement.get("Version"):
            replacement_path = module_name(replacement.get("Path"))
            replacement_version = replacement["Version"]
            require(isinstance(replacement_version, str) and
                    re.fullmatch(r"v[A-Za-z0-9_.+-]+", replacement_version),
                    "invalid_replacement_version")
            effective = {"kind": "versioned_module", "path": replacement_path,
                         "version": replacement_version}
        error = GateError("unexpected_module_replacement")
        error.safe_diagnostics = {"original_module": {"path": path, "version": version},
                                  "effective_module": effective}
        raise error
    require(obj.get("Sum") == sums.get((path, version)) and obj.get("Sum"), "module_sum_mismatch")
    require(obj.get("GoModSum") == sums.get((path, version + "/go.mod")) and obj.get("GoModSum"),
            "module_modsum_mismatch")
    root = Path(obj.get("Dir", "")).resolve()
    require(root.is_dir() and root.is_relative_to((state / "modcache").resolve()), "module_dir_escape")
    if path in PINS:
        require((version, obj["Sum"], obj["GoModSum"]) == PINS[path], "pin_mismatch")
    identity = {"path": path, "version": version, "sum": obj["Sum"], "go_mod_sum": obj["GoModSum"]}
    return {"original": identity, "effective": dict(identity), "replacement": False}, root


def file_record(root, path, budget=None, deadline=None):
    require(deadline is None or time.monotonic() < deadline, "deadline")
    name = str(path.relative_to(root))
    data = read_file(root, name)
    if budget is not None:
        budget["bytes"] += len(data)
        require(budget["bytes"] <= MAX_SOURCE_BYTES, "source_byte_limit")
    require(deadline is None or time.monotonic() < deadline, "deadline")
    return {"path": name, "sha256": sha(data)}


def found_licenses(root, directories, budget=None, deadline=None):
    result = {}
    for directory in directories:
        current = directory
        while current.is_relative_to(root):
            for p in current.iterdir():
                if p.is_file() and re.match(r"(?i)^(license|licence|copying|notice)([._-].*)?$", p.name):
                    if str(p.relative_to(root)) in result:
                        continue
                    rec = file_record(root, p, budget, deadline)
                    result[rec["path"]] = rec
            if current == root:
                break
            current = current.parent
    return [result[k] for k in sorted(result)]


def inventory(packages, modules, roots, work, state, goroot, deadline=None):
    seen = set()
    result = []
    source_count = 0
    source_budget = {"bytes": 0}
    directories = {"std": set(), "main": set()}
    component_roots = {"std": goroot, "main": work}
    for name, root in roots.items():
        component_roots[name] = root
        directories[name] = set()
    for obj in packages:
        require(deadline is None or time.monotonic() < deadline, "deadline")
        require(not obj.get("Error") and not obj.get("DepsErrors") and not obj.get("Incomplete"), "package_error")
        package_id = identifier(obj.get("ImportPath"))
        require(package_id not in seen, "duplicate_package_instance")
        seen.add(package_id)
        for_test = obj.get("ForTest", "")
        require(for_test in ("", PROBE), "upstream_tests_selected")
        require(isinstance(obj.get("Imports", []), list) and isinstance(obj.get("ImportMap", {}), dict),
                "invalid_import_metadata")
        imports = [identifier(x) for x in obj.get("Imports", [])]
        mapping = {identifier(k): identifier(v) for k, v in obj.get("ImportMap", {}).items()}
        item = {"id": package_id, "for_test": for_test, "imports": imports, "import_map": mapping}
        if package_id == PROBE + ".test":
            require(obj.get("Name") == "main", "invalid_testmain")
            generated = []
            require(len(obj.get("GoFiles", [])) == 1, "unexpected_testmain_sources")
            for name in obj["GoFiles"]:
                p = Path(name)
                require(p.name == "_testmain.go" or re.fullmatch(r"[0-9a-f]{64}-d", p.name), "unknown_generated_source")
                if not p.is_absolute() and obj.get("Dir"):
                    p = Path(obj["Dir"]) / p
                if p.is_file():
                    require(not p.is_symlink() and p.stat().st_size <= MAX_FILE_BYTES,
                            "unsafe_generated_source")
                    require(p.resolve().is_relative_to(state.resolve()), "generated_source_escape")
                    generated.append({"logical_name": "_testmain.go", "materialized": True,
                                      "sha256": file_record(state, p, source_budget, deadline)["sha256"]})
                else:
                    require(name == "_testmain.go" or (p.is_absolute() and p.is_relative_to(state)),
                            "unknown_missing_generated_source")
                    generated.append({"logical_name": "_testmain.go", "materialized": False,
                                      "sha256": None, "reason": "go_list_did_not_materialize"})
            item.update(component="main", files=[], generated_sources=generated)
            result.append(item)
            continue
        if obj.get("Standard"):
            component, root = "std", goroot
        elif obj.get("Module", {}).get("Main"):
            require(obj["Module"].get("Path") == MAIN and package_id in (PROBE, PROBE + " [" + PROBE + ".test]"),
                    "unexpected_main_package")
            component, root = "main", work
        else:
            component = module_name(obj.get("Module", {}).get("Path"))
            require(component in modules, "unknown_package_module")
            root = roots[component]
            require(obj["Module"].get("Version") == modules[component]["original"]["version"] and
                    not obj["Module"].get("Replace"), "package_module_identity_mismatch")
        directory = Path(obj.get("Dir", "")).resolve()
        require(directory.is_dir() and directory.is_relative_to(root.resolve()), "package_dir_escape")
        directories[component].add(directory)
        files = []
        fields = list(FILE_FIELDS)
        if package_id == PROBE:
            require(obj.get("TestImports") == [BLS + "/crypto/bls/blst"], "probe_import_mismatch")
            fields.append("TestGoFiles")
        for field in fields:
            for name in obj.get(field, []):
                p = directory / relative(name)
                rec = file_record(root, p, source_budget, deadline)
                rec["role"] = field
                source_count += 1
                require(source_count <= 4096, "source_file_limit")
                files.append(rec)
        require(files, "missing_package_sources")
        item.update(component=component, files=files, generated_sources=[])
        result.append(item)
    require(PROBE in seen and PROBE + ".test" in seen and
            PROBE + " [" + PROBE + ".test]" in seen, "missing_test_graph_instances")
    require(BLS + "/crypto/bls/blst" in seen and BLS + "/cache/nonblocking" in seen,
            "missing_baseline_bls")
    require(BLS + "/crypto/bls" not in seen and
            not any(x.startswith(BLS + "/crypto/bls/herumi") for x in seen),
            "unexpected_bls_wrapper")
    for item in result:
        for imported in item["imports"]:
            resolved = item["import_map"].get(imported, imported)
            require(resolved == "C" or resolved in seen, "unresolved_import_instance")
    licenses = {component: found_licenses(component_roots[component], dirs | {component_roots[component]}, source_budget, deadline)
                for component, dirs in directories.items()}
    return result, licenses


def collect(root, go, runner=capture):
    deadline = time.monotonic() + TOTAL_SECONDS
    go_digest = sha(read_file(go.parent, go.name))
    original_inputs = input_hashes(root)
    sums = sum_entries(read_file(root, "go.sum").decode())
    for path, (version, checksum, modsum) in PINS.items():
        require(sums.get((path, version)) == checksum and sums.get((path, version + "/go.mod")) == modsum,
                "declared_pin_mismatch")
    with tempfile.TemporaryDirectory(prefix="bls-stage0-") as temp:
        state = Path(temp).resolve()
        work = state / "work"
        work.mkdir()
        for directory in ("home", "tmp", "buildcache", "modcache"):
            (state / directory).mkdir()
        for name in INPUTS:
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(read_file(root, name))
        target = work / "internal/blsgraphprobe/graph_test.go"
        target.parent.mkdir(parents=True)
        target.write_bytes(read_file(root, PROBE_FILE))
        env = clean_env(go, state)
        info_objects = stream_json(runner([str(go), "env", "-json", *ENV_KEYS], work, env, deadline))
        require(len(info_objects) == 1 and set(info_objects[0]) == set(ENV_KEYS), "go_environment_shape")
        info = info_objects[0]
        checks = {"GOVERSION": GO_VERSION, "GOOS": "linux", "GOARCH": "amd64", "GOAMD64": "v1",
                  "GOHOSTOS": "linux", "GOHOSTARCH": "amd64", "CGO_ENABLED": "1",
                  "GOMOD": str(work / "go.mod"), "GOFLAGS": FLAGS,
                  "GOPROXY": "https://proxy.golang.org", "GOSUMDB": "off", "GOTOOLCHAIN": "local",
                  "GOMODCACHE": str(state / "modcache"), "GOCACHE": str(state / "buildcache")}
        require(all(info.get(k) == v for k, v in checks.items()) and info.get("GOWORK") in ("", "off"),
                "go_environment_mismatch")
        goroot = Path(info.get("GOROOT", "")).resolve()
        require(goroot.is_dir() and goroot == go.parent.parent.resolve(), "go_root_mismatch")
        version_bytes = read_file(goroot, "VERSION")
        version_lines = version_bytes.decode("utf-8").splitlines()
        require(bool(version_lines) and version_lines[0] == GO_VERSION, "go_root_version_mismatch")
        requests = [name + "@" + PINS[name][0] for name in sorted(PINS)]
        downloads = stream_json(runner([str(go), "mod", "download", "-json", *requests], work, env, deadline))
        require({x.get("Path") for x in downloads} == set(PINS) and len(downloads) == len(PINS), "download_identity_mismatch")
        for obj in downloads:
            require(not obj.get("Error"), "download_failed")
            version, checksum, modsum = PINS[obj["Path"]]
            require((obj.get("Version"), obj.get("Sum"), obj.get("GoModSum")) == (version, checksum, modsum),
                    "download_checksum_mismatch")
        packages = stream_json(runner([str(go), "list", "-deps", "-test", "-json",
                                      "./internal/blsgraphprobe"], work, env, deadline))
        used = {module_name(p["Module"]["Path"]) for p in packages
                if p.get("Module") and not p["Module"].get("Main")}
        names = sorted(used | set(PINS))
        module_objects = stream_json(runner([str(go), "list", "-m", "-json", *names], work, env, deadline))
        require(len(module_objects) == len(names) and {x.get("Path") for x in module_objects} == set(names),
                "module_inventory_mismatch")
        modules, roots = {}, {}
        for obj in module_objects:
            rec, module_root = module_record(obj, sums, state)
            rec["has_selected_packages"] = obj["Path"] in used
            modules[obj["Path"]], roots[obj["Path"]] = rec, module_root
        prysm_hashes = {p: sha(read_file(roots[BLS], p)) for p in PRYSM_FILES}
        require(prysm_hashes == PRYSM_FILES, "upstream_source_mismatch")
        records, licenses = inventory(packages, modules, roots, work, state, goroot, deadline)
        native_refs = [file_record(roots[BLST], roots[BLST] / name)
                       for name in ("bindings/blst.h", "src/server.c", "build/assembly.S")]
        require(all(sha(read_file(work, name)) == expected for name, expected in INPUTS.items()),
                "temporary_lock_or_compat_changed")
        require(sha(target.read_bytes()) == PROBE_SHA, "temporary_probe_changed")
        require(input_hashes(root) == original_inputs, "source_inputs_changed")
        require(sha(read_file(go.parent, go.name)) == go_digest, "go_binary_changed")
        require(read_file(goroot, "VERSION") == version_bytes, "go_root_version_changed")
        report = {
            "schema": "bls-stage0-inventory-v1", "status": "metadata_inputs_verified",
            "scope": "single_import_only_probe_not_six_or_ten_regression_test_graph",
            "tests_executed": False, "patch_applied": False, "license_approval": False,
            "build_mode": "non_race_metadata_only",
            "native_include_closure_proven": False,
            "baseline_public_commit": "560ddfc85e05f9a22d16d3b420396d10605e00c8",
            "baseline_local_source_commit": "df604e0453d580217071cd189e8cf1e5054aa196",
            "baseline_identity_note": "Parent-confirmed source equivalence; exact input hashes are authoritative, not identical Git identity.",
            "tool": {"version": GO_VERSION, "go_binary_sha256": go_digest,
                     "goroot_version_file_sha256": sha(version_bytes),
                     "complete_toolchain_attestation": False,
                     "goos": "linux", "goarch": "amd64", "goamd64": "v1", "cgo_enabled": True},
            "main_module": MAIN, "input_sha256": original_inputs,
            "original_prysm_file_sha256": prysm_hashes,
            "modules": modules, "packages": records, "found_license_files": licenses,
            "native_reference_files_not_complete_include_closure": {BLST: native_refs},
            "observations": {
                "prysm_build_bazel_present": any(p["id"].startswith(BLS + "/build/bazel") for p in records),
                "prysm_ssz_equality_present": any("equality" in p["id"] and p["id"].startswith(BLS + "/encoding/ssz") for p in records),
                "nonblocking_present": True,
            },
        }
        require(time.monotonic() < deadline, "deadline")
        encoded = json.dumps(report, sort_keys=True, indent=2) + "\n"
        require(len(encoded.encode()) <= MAX_REPORT, "report_limit")
        return encoded


def main():
    try:
        require(len(sys.argv) == 1, "unknown_argument")
        reject_environment(os.environ)
        executable = shutil.which("go")
        require(executable is not None, "missing_go")
        output = collect(Path(__file__).resolve().parents[1], Path(executable).resolve())
    except (GateError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, GateError) else "metadata_collection_error"
        diagnostics = getattr(error, "safe_diagnostics", None)
        suffix = " " + json.dumps(diagnostics, sort_keys=True) if diagnostics else ""
        print("BLS_STAGE0_ERROR:" + code + suffix, file=sys.stderr)
        return 1
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
