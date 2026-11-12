# SPDX-License-Identifier: GPL-3.0-or-later
import json
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import scanner_redgreen_ci as rg


def stream(red):
    ev = [{"Action": "start", "Package": rg.PACKAGE}]
    for name in rg.NAMES:
        ev.append({"Action": "run", "Test": name, "Package": rg.PACKAGE})
        fail = red and name in {rg.CANCEL, rg.TRACE, rg.TRACE+"/tests"}
        message = None
        if red and name == rg.CANCEL:
            message = "callgraph_error_test.go:60: source returned result=<nil> error=<nil>; want nil, context.Canceled"
        if red and name == rg.TRACE+"/tests":
            message = "test_only_symbol_trace_test.go:70: no ordered TestOnlyTarget -> wrapper -> Target witness"
        if message:
            ev.append({"Action": "output", "Test": name, "Package": rg.PACKAGE, "Output": "    "+message+"\n"})
        if name != rg.TRACE:
            ev.append({"Action": "fail" if fail else "pass", "Test": name, "Package": rg.PACKAGE})
    ev.append({"Action": "fail" if red else "pass", "Test": rg.TRACE, "Package": rg.PACKAGE})
    ev.append({"Action": "fail" if red else "pass", "Package": rg.PACKAGE})
    return ev


def encode(events):
    return "\n".join(json.dumps(e) for e in events)+"\n"


class Events(unittest.TestCase):
    def test_exact_controls(self):
        for red in (False, True):
            rg.classify(encode(stream(red)), int(red), red)

    def test_faults(self):
        mutations = {
            "missing test": lambda es: [e for e in es if e.get("Test") != rg.CANCEL],
            "wrong package": lambda es: [dict(e, Package="other") for e in es],
            "missing marker": lambda es: [e for e in es if e["Action"] != "output"],
            "wrong marker": lambda es: [dict(e, Output="    callgraph_error_test.go:60: another assertion\n") if e["Action"] == "output" else e for e in es],
            "skip": lambda es: [dict(e, Action="skip") if e["Action"] == "fail" else e for e in es],
            "all green baseline": lambda es: stream(False),
            "extra fail": lambda es: es[:-1]+[{"Package":rg.PACKAGE,"Test":"Unknown","Action":"fail"}]+es[-1:],
            "duplicate marker": lambda es: [dict(e, Output=e["Output"]*2) if e["Action"] == "output" else e for e in es],
            "package missing terminal": lambda es: es[:-1],
            "duplicate terminal": lambda es: es+[es[-1]],
            "fail before run": lambda es: [es[0],es[2],es[1]]+es[3:],
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                rg.classify(encode(mutate(stream(True))), 1, True)

    def test_wrong_exit_build_and_crash(self):
        for text, code in [
            (encode(stream(True)), 2), ("build failed\n", 1),
            ("", 1), (encode(stream(True))+"panic: bad\n", 1),
            (encode(stream(True))+"WARNING: DATA RACE\n", 1),
            (encode(stream(True))+"test timed out\n", 1),
        ]:
            with self.subTest(code=code,text=text[:30]), self.assertRaises((ValueError,json.JSONDecodeError)):
                rg.classify(text, code, True)

    def test_unexpected_green_failure(self):
        with self.assertRaises(ValueError):
            rg.classify(encode(stream(True)), 0, False)

    def test_only_two_tests_selected(self):
        self.assertEqual(len(rg.TEST_FILES), 2)
        self.assertEqual(rg.PATTERN, "^(TestCallgraphCancellationPropagates|TestTestOnlySymbolTraceControl)$")


class Capture(unittest.TestCase):
    def invoke(self, code, budget=None, timeout=2):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            try:
                result=rg.capture([sys.executable,"-c",code],root,dict(os.environ),root,"cmd",budget or rg.Budget(3),timeout)
            except ValueError as exc:
                if (root/"cmd.json").exists():
                    status=json.loads((root/"cmd.json").read_text())
                    self.assertIsNotNone(status["returncode"], "direct child was not reaped")
                    self.assertTrue((root/"cmd.log").exists())
                    raw=(root/"cmd.log").read_bytes()
                    self.assertEqual(len(raw),status["bytes"])
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), status["sha256"])
                    if status["reason"] == "output_limit":
                        self.assertTrue(raw and len(raw) <= rg.COMMAND_BYTES)
                else:
                    self.assertIn("global budget", str(exc))
                raise
            return result

    def test_binary_identity_requires_regular_non_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            binary=root/"fixture.test"
            binary.write_bytes(b"first")
            before=rg.binary_hash(binary)
            binary.write_bytes(b"changed")
            self.assertNotEqual(before,rg.binary_hash(binary))
            alias=root/"alias";alias.symlink_to(binary)
            for p in (root,alias,root/"missing"):
                with self.assertRaises(ValueError): rg.binary_hash(p)

    def test_nonzero_preserved_not_accepted(self):
        code,text=self.invoke("print('evidence');raise SystemExit(1)")
        self.assertEqual((code,text),(1,"evidence\n"))

    def test_real_timeout_reaps_even_before_child_can_print(self):
        with self.assertRaisesRegex(ValueError,"timeout"):
            self.invoke("import time;print('prefix',flush=True);time.sleep(20)",timeout=.15)

    def test_received_prefix_survives_timeout_deterministically(self):
        # Startup scheduling does not promise output within 150 ms. Model one
        # completed pipe read followed by deadline expiry, without a real child
        # or any change to the production timeout/capture implementation.
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            proc=Mock(pid=4242,returncode=None)
            proc.stdout.fileno.return_value=123
            proc.poll.side_effect=lambda: proc.returncode
            def reaped(**kwargs):
                proc.returncode=-15
                return -15
            proc.wait.side_effect=reaped
            selector=Mock()
            selector.get_map.return_value={123: True}
            selector.select.return_value=[(Mock(fileobj=proc.stdout),rg.selectors.EVENT_READ)]
            budget=Mock(deadline=10.0,remaining=1024)
            with patch.object(rg.subprocess,"Popen",return_value=proc), \
                 patch.object(rg.selectors,"DefaultSelector",return_value=selector), \
                 patch.object(rg.os,"read",return_value=b"prefix\n"), \
                 patch.object(rg.time,"monotonic",side_effect=[0.0,0.0,0.0,0.0,1.0]), \
                 patch.object(rg.os,"killpg") as kill, \
                 self.assertRaisesRegex(ValueError,"timeout"):
                rg.capture(["mock-child"],root,{},root,"cmd",budget,.15)
            raw=(root/"cmd.log").read_bytes()
            status=json.loads((root/"cmd.json").read_text())
            self.assertEqual(raw,b"prefix\n")
            self.assertEqual(status["bytes"],len(raw))
            self.assertEqual(status["sha256"],hashlib.sha256(raw).hexdigest())
            self.assertEqual((status["reason"],status["returncode"]),("timeout",-15))
            self.assertEqual(kill.call_args_list,[call(4242,rg.signal.SIGTERM),call(4242,rg.signal.SIGKILL)])
            self.assertEqual(proc.wait.call_args_list,[call(timeout=2),call(timeout=3)])
            proc.stdout.close.assert_called_once()
            selector.close.assert_called_once()

    def test_output_cap_preserves_prefix_reaps(self):
        with patch.object(rg,"COMMAND_BYTES",32), self.assertRaisesRegex(ValueError,"output_limit"):
            self.invoke("print('x'*1000)")

    def test_kill_group_even_after_leader_exits(self):
        proc=Mock(pid=4242)
        proc.wait.return_value=0
        with patch.object(rg.os,"killpg") as kill:
            self.assertFalse(rg.terminate_group(proc))
        self.assertEqual(kill.call_args_list,[call(4242,rg.signal.SIGTERM),call(4242,rg.signal.SIGKILL)])
        self.assertEqual(proc.wait.call_args_list,[call(timeout=2),call(timeout=3)])

    def test_global_budget(self):
        with self.assertRaisesRegex(ValueError,"exhausted"):
            self.invoke("print('not run')",budget=rg.Budget(seconds=-1))
        with self.assertRaisesRegex(ValueError,"output_limit"):
            self.invoke("print('x'*1000)",budget=rg.Budget(limit=16))



def framework_line(value, duration=False):
    phase = "2.69244ms for" if duration else "starting"
    return ("    invoke.go:205: "+phase+" GOROOT= GOPATH=/fixture/modcache GO111MODULE=on "
            +"GOPROXY=file:///fixture/modproxy "+value
            +' go list -f "{{context.GOARCH}} {{context.Compiler}}" -- unsafe\n')


def with_framework(events, value):
    output=[]
    for e in events:
        output.append(e)
        if e["Action"]=="run" and e.get("Test") in {rg.CANCEL,rg.TRACE+"/ordinary",rg.TRACE+"/tests"}:
            for duration in (False,True):
                output.append({"Action":"output","Package":rg.PACKAGE,"Test":e["Test"],
                               "Output":framework_line(value,duration)})
    return output


class FrameworkDiagnostics(unittest.TestCase):
    def test_observed_shapes_and_masked_semantics(self):
        for value in ("PUBLIC_PLACEHOLDER","***"):
            outcome=rg.classify(encode(with_framework(stream(True),value)),1,True)
            self.assertEqual(outcome["framework_diagnostic_counts"][rg.CANCEL],2)
            self.assertNotIn(value,json.dumps(outcome))
        for name in (rg.CANCEL,rg.TRACE+"/ordinary",rg.TRACE+"/tests"):
            tests=name!=rg.TRACE+"/ordinary"
            fields=rg.JSON_FIELDS_TEST if tests else rg.JSON_FIELDS_ORDINARY
            suffix="/entry/x" if name==rg.CANCEL else "/entry/p"
            line=("    invoke.go:205: starting GOROOT= GOPATH=/fixture/modcache GO111MODULE=on GOPROXY=file:///fixture/modproxy *** go list -e -json="
                  +fields+" -compiled=true -test="+str(tests).lower()
                  +" -export=false -deps=true -find=false -buildvcs=false -pgo=off -- /fixture"+suffix)
            self.assertTrue(rg.framework_diagnostic(line,name))

    def test_unknown_and_extra_assertions_still_fail(self):
        for bad in (
            "    invoke.go:205: unknown fatal diagnostic\n",
            "    invoke.go:206: starting GOROOT= GOPATH=x GO111MODULE=on GOPROXY=file://x go list -e -f {{context.ReleaseTags}} -- unsafe\n",
            "    unknown.go:9: extra assertion\n",
            framework_line("x").rstrip()+"\n    unknown.go:9: extra assertion\n",
        ):
            events=with_framework(stream(True),"***")
            events.insert(2,{"Action":"output","Package":rg.PACKAGE,"Test":rg.CANCEL,"Output":bad})
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                rg.classify(encode(events),1,True)

    def test_valid_framework_does_not_hide_wrong_marker(self):
        events=with_framework(stream(True),"***")
        for e in events:
            if e["Action"]=="output" and "callgraph_error_test.go:" in e["Output"]:
                e["Output"]="    callgraph_error_test.go:61: wrong failure\n"
        with self.assertRaises(ValueError):rg.classify(encode(events),1,True)


class ExportAndWorkflow(unittest.TestCase):
    CANARY = "DO_NOT_EXPORT_ENV_CANARY"

    def evidence(self, root):
        identity = {"upstream": rg.base.UPSTREAM, "go": rg.base.GO_VERSION,
                    "test_hashes": {p:"0"*64 for p in rg.TEST_FILES},
                    "patched_source": {p:"1"*64 for p in rg.base.CHANGED},
                    "compiled_package_binaries": {"baseline":"2"*64,"patched":"3"*64}}
        (root/"identity-before.json").write_text(json.dumps(identity))
        after=dict(identity, post_execution_binaries=identity["compiled_package_binaries"], environment=self.CANARY)
        (root/"result.json").write_text(json.dumps(after))
        isolation={"uid":1001,"groups":[],"network_namespace":self.CANARY,
                   "host_network_namespace":"different","verified_down_loopback_and_empty_routes":True,
                   "status":{k:"0" for k in ("CapInh","CapPrm","CapEff","CapBnd","CapAmb")}}
        isolation["status"]["NoNewPrivs"]="1"
        (root/"isolation.json").write_text(json.dumps(isolation))
        for side in ("baseline","patched"):
            for phase in ("compile","tests"):
                log="" if phase=="compile" else encode(with_framework(stream(side=="baseline"),self.CANARY))
                label=side+"-"+phase
                raw=log.encode()
                (root/(label+".log")).write_bytes(raw)
                (root/(label+".json")).write_text(json.dumps({
                    "command":[self.CANARY],"reason":None,"returncode":int(side=="baseline" and phase=="tests"),
                    "bytes":len(raw),"sha256":rg.hashlib.sha256(raw).hexdigest()}))

    def test_semantic_export_exact_hash_and_no_raw_canary(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.evidence(root)
            (root/"outer.log").write_text(self.CANARY)
            sink=io.StringIO();result=rg.export_evidence(root,True,sink)
            self.assertNotIn(self.CANARY,sink.getvalue())
            self.assertNotIn("invoke.go",sink.getvalue())
            self.assertNotIn('"Output"',sink.getvalue())
            record=json.loads(sink.getvalue())
            self.assertEqual(record["evidence_format"],rg.SAFE_FORMAT)
            payload=json.dumps(record["payload"],sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()
            self.assertEqual(len(payload),record["sanitized_bytes"])
            self.assertEqual(rg.hashlib.sha256(payload).hexdigest(),record["sanitized_sha256"])
            self.assertEqual(result["exported_bytes"],len(sink.getvalue().encode()))
            for side in ("baseline","patched"):
                self.assertTrue(record["payload"]["tests"][side]["classification"]["valid"])
                self.assertIn("raw_local_sha256",record["payload"]["tests"][side]["execution"])

    def test_missing_or_output_cap_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            with self.assertRaises(ValueError):rg.export_evidence(root,True,io.StringIO())
            self.evidence(root)
            with self.assertRaisesRegex(ValueError,"output limit"):
                rg.export_evidence(root,True,io.StringIO(),limit=3072)

    def test_invalid_output_exports_only_safe_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.evidence(root)
            events=stream(True)
            for e in events:
                if e.get("Test")==rg.CANCEL and e["Action"]=="output":
                    e["Output"]="    unknown.go:99: "+self.CANARY+"\n"
            raw=encode(events).encode()
            (root/"baseline-tests.log").write_bytes(raw)
            status=json.loads((root/"baseline-tests.json").read_text())
            status.update(bytes=len(raw),sha256=rg.hashlib.sha256(raw).hexdigest())
            (root/"baseline-tests.json").write_text(json.dumps(status))
            sink=io.StringIO();rg.export_evidence(root,False,sink)
            self.assertNotIn(self.CANARY,sink.getvalue())
            classification=json.loads(sink.getvalue())["payload"]["tests"]["baseline"]["classification"]
            self.assertEqual(classification,{"valid":False,"error_code":"test_evidence_rejected"})
            with self.assertRaises(ValueError):rg.export_evidence(root,True,io.StringIO())

    def test_masked_copy_cannot_claim_original_log_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.evidence(root)
            p=root/"baseline-tests.log"
            p.write_text(p.read_text().replace(self.CANARY,"***"))
            with self.assertRaisesRegex(ValueError,"digest mismatch"):
                rg.export_evidence(root,False,io.StringIO())

    def test_export_rejects_symlink_and_oversize(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);outside=root/"not-allowlisted";outside.write_text(self.CANARY)
            p=root/"result.json";p.symlink_to(outside)
            with self.assertRaisesRegex(ValueError,"unsafe"):
                rg.export_evidence(root,False,io.StringIO())
            p.unlink();p.write_text("x"*100)
            with patch.object(rg,"COMMAND_BYTES",32),self.assertRaisesRegex(ValueError,"oversized"):
                rg.export_evidence(root,False,io.StringIO())

    def test_workflow_appends_comparison_without_replacing_original_tests(self):
        text=(Path(__file__).resolve().parents[1]/".github/workflows/ci.yml").read_text()
        jobs=[line.strip()[:-1] for line in text.splitlines() if line.startswith("  ") and not line.startswith("   ") and line.endswith(":")]
        self.assertEqual(jobs,["test","official_txpool","canonical_isolation","noexecstack_candidate","scanner_synthetic","bls_stage0_inventory","bls_cache_race"])
        job=text.split("  scanner_synthetic:",1)[1].split("\n  bls_stage0_inventory:",1)[0]
        self.assertIn("timeout-minutes: 15",job)
        first=job.index('scripts/scanner_synthetic_ci.py run')
        second=job.index('scripts/scanner_redgreen_ci.py run')
        self.assertLess(first,second)
        self.assertIn('timeout-minutes: 9',job[first:])
        self.assertIn('"$RUNNER_TEMP/scanner-redgreen"',job[second:])
        self.assertNotIn('continue-on-error',job)
        self.assertNotIn('if: always()',job)
        self.assertEqual(len(rg.base.TESTS['./internal/vulncheck'])+len(rg.base.TESTS['./internal/scan']),11)



if __name__ == "__main__":
    unittest.main()
