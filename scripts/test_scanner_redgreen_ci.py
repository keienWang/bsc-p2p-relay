# SPDX-License-Identifier: GPL-3.0-or-later
import json
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
                    if status["reason"] == "timeout":
                        self.assertTrue(raw.startswith(b"prefix"))
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

    def test_timeout_preserves_prefix_reaps(self):
        with self.assertRaisesRegex(ValueError,"timeout"):
            self.invoke("import time;print('prefix',flush=True);time.sleep(20)",timeout=.15)

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


class ExportAndWorkflow(unittest.TestCase):
    def evidence(self, root):
        for name in rg.REQUIRED_EXPORT:
            (root/name).write_text("test2json exact UTF-8: \u4e2d\n")

    def test_export_exact_roundtrip_and_allowlist(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.evidence(root)
            (root/"private-source.go").write_text("must not export")
            sink=io.StringIO();result=rg.export_evidence(root,True,sink)
            records=[json.loads(s) for s in sink.getvalue().splitlines()]
            self.assertEqual({r["evidence_file"] for r in records},rg.REQUIRED_EXPORT)
            self.assertEqual(result["exported_bytes"],len(sink.getvalue().encode()))
            for record in records:
                raw=(root/record["evidence_file"]).read_bytes()
                self.assertTrue(record["complete"])
                self.assertEqual(record["text"].encode(),raw)
                self.assertEqual(record["sha256"],rg.hashlib.sha256(raw).hexdigest())

    def test_export_missing_or_truncated_success_is_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            with self.assertRaisesRegex(ValueError,"missing or truncated"):
                rg.export_evidence(root,True,io.StringIO())
            self.evidence(root)
            (root/"baseline-tests.log").write_text('"'*5000)
            with self.assertRaisesRegex(ValueError,"missing or truncated"):
                rg.export_evidence(root,True,io.StringIO(),limit=3072)

    def test_export_failure_prefix_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/"baseline-tests.log").write_text("prefix"*5000)
            sink=io.StringIO();result=rg.export_evidence(root,False,sink,limit=3072)
            self.assertLessEqual(len(sink.getvalue().encode()),1024)
            self.assertEqual(result["incomplete_files"],["baseline-tests.log"])
            record=json.loads(sink.getvalue());self.assertFalse(record["complete"])
            self.assertTrue(record["text"].startswith("prefix"))

    def test_export_rejects_symlink_and_oversize(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);outside=root/"not-allowlisted";outside.write_text("secret")
            p=root/"result.json";p.symlink_to(outside)
            with self.assertRaisesRegex(ValueError,"unsafe evidence"):
                rg.export_evidence(root,False,io.StringIO())
            p.unlink();p.write_text("x"*100)
            with patch.object(rg,"COMMAND_BYTES",32),self.assertRaisesRegex(ValueError,"oversized evidence"):
                rg.export_evidence(root,False,io.StringIO())

    def test_workflow_appends_comparison_without_replacing_original_tests(self):
        text=(Path(__file__).resolve().parents[1]/".github/workflows/ci.yml").read_text()
        jobs=[line.strip()[:-1] for line in text.splitlines() if line.startswith("  ") and not line.startswith("   ") and line.endswith(":")]
        self.assertEqual(jobs,["test","official_txpool","canonical_isolation","noexecstack_candidate","scanner_synthetic"])
        job=text.split("  scanner_synthetic:",1)[1]
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
