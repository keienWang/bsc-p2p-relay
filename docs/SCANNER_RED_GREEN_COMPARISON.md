# Strict scanner old-red/new-green comparison (implementation, not execution)

The initial harness was reviewed on f6296b97dd5945a6a2b022b843783ecd8c0a4efb. This integration is based on 847ba30de02eee5af91174cc76c7a32ba9dfc200, preserving its no-executable-stack work. It appends a comparison step to the existing scanner_synthetic job after the original 11 synthetic tests pass. All five jobs remain; original tests, scanner harness/patches, relay runtime, dependency files and production scanner entry point are unchanged.

## Inputs and boundary

Use an existing successful scanner_synthetic_ci.py prepare receipt and its normalized patched source, with the same fixed official golang/vuln commit e672cbe8fbc8c76e28fcb6db0efd9ce354e0dac5 and Go 1.26.11.
The new script has no prepare/download phase. Within the disposable, verified network/PID namespace it creates a local no-hardlink clone of the donor's committed official tree. Only the two normalized test files are copied into that baseline:
- internal/vulncheck/callgraph_error_test.go
- internal/vulncheck/test_only_symbol_trace_test.go

Both sides use byte-identical test files, checked by SHA256. The baseline must remain the original tracked tree with exactly those two untracked additions. The patched side must retain the original receipt hashes and exact changed-file scope before/after. Binary hashes, source/test identities, commands, exit status and raw outputs are retained in a fresh attempt directory. Both binaries are required to remain regular non-symlink files with identical hashes after execution; both tree HEADs are explicitly rechecked against the fixed upstream commit. These checks are not an adversarial-filesystem proof.

The harness reuses the existing clean environment, bundle validation and isolation checks: non-root uid/gid, zero capabilities/supplementary groups, no-new-privs, changed network namespace, down-only loopback and no routes. Module proxy/sumdb, automatic toolchain and telemetry are off. There is no real DB client or govulncheck CLI invocation in this harness. The selected tests use NewInMemoryClient; generated packagestest fixture modules can use their local file proxy, not external network.

## Compile and run contract

It first compiles **two test binaries total**, one for internal/vulncheck in each tree, with -race and -mod=readonly. Neither test binary runs until both compilations succeed.
It then runs only TestCallgraphCancellationPropagates and TestTestOnlySymbolTraceControl through go tool test2json; the latter includes ordinary/tests subcases. It does not run all upstream tests.

Baseline acceptance is intentionally narrow:
- Cancellation test fails with its single exact assertion: source returned result=<nil> error=<nil>; want nil, context.Canceled.
- The ordinary subcase passes.
- The tests subcase fails with its single exact assertion: no ordered TestOnlyTarget -> wrapper -> Target witness.
- The trace parent and package fail accordingly; the process exits 1.

The patched side must pass all four named test/subtest events and the package, with process exit 0. The parser requires exact names, terminals and package identity; excludes missing/duplicate/unselected/skip events and any other located assertion. Panic, timeout, data race, compile failure or arbitrary non-JSON error are never accepted as baseline red. The previous formatting failure is not algorithm-red evidence.

## Bounded resources and failure evidence

- Serial comparison; GOMAXPROCS=2 and Go build -p=1.
- 480-second inner aggregate budget and namespace watchdog, TERM then KILL after 10 seconds; outer capture additionally bounded at 500 seconds within a 510-second budget.
- Each compile is capped at 240 seconds within the aggregate; each test binary has a 150-second Go timeout and 165-second capture cap.
- Per-command captured output: 16 MiB; aggregate inner output: 32 MiB. Outer capture independently bounded. Binary hashing streams rather than reading the entire binary into memory.
- On capture timeout/overflow the output prefix and status are saved. TERM and KILL are both sent to the process group even if the leader exited early, with finite waits. The direct child is reaped; remaining descendant lifecycle belongs to the outer PID namespace. This is not a claim of individual descendant reaping or a proven memory/RSS bound.
- A new attempt directory is mandatory. Failed attempts are not silently reused. Result success is written only after both compiled binaries satisfy their exact opposite contracts and source rechecks.

## Reproducible checks completed locally

Python-only command:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -m unittest discover -s scripts -p 'test_scanner*ci.py' -v

27 tests passed: 11 new comparison contracts plus 16 existing synthetic harness contracts (0.434 seconds). The previous 26-test pass is preserved; the final rerun additionally verifies binary hash drift and non-regular/symlink rejection. New contracts include malformed/missing/duplicate events, wrong assertions and exits, build/panic/race/timeout rejection, real bounded Python subprocess captures, and leader-exits-before-KILL mocking.
Raw local log SHA256:
6c86d41a52b618e1ccf5a7abbe710c7325310263d2930b6b7fc1eb266e44b22f

No Go command, real namespace, scanner binary, old-red/new-green run, DB/network access or hosted CI was executed for this batch. Both required Go binaries remain **uncompiled in this batch**. These Python results establish harness fault contracts only, not the algorithm comparison or a production security claim.

## Hosted evidence export and integration

The added comparison step has a 9-minute timeout; the namespace retains its 480-second watchdog. The existing job remains at 15 minutes. The cumulative cold-run budget is NOT proven; exhaustion must fail rather than weaken tests or extend coverage claims.

After either comparison success or failure, a fixed allowlist exports only isolation/receipt/status files and test/compile logs. It never traverses or dumps the baseline clone. Each stdout JSON record names the file and includes original byte length, SHA256, an explicit completeness flag and exact UTF-8 text (JSON-escaped, reversible). Missing/truncated required success evidence fails the outer run. Failures can export marked prefixes; those are not complete logs. Binary files and upstream source are never exported.

Export is capped at 32 MiB, reserving 2 KiB for the final outer receipt and bounded error line. Unexpected oversized/symlink files fail closed. The final outer_result record distinguishes comparison_passed from passed: an inner success with failed export is NOT a successful job. The corresponding outer-result.json is written only after export is attempted. Platform-level job termination can still prevent final output; absence of the final receipt is never success.

The original 27-test Python-only evidence above is retained as historical scope. The integration adds exporter/workflow contracts and reruns the complete Python suite; the final count/hash are recorded below. No local Go compilation, real namespace, old-red/new-green run or hosted execution is claimed for this integration.

Final integration validation: complete Python suite **118 tests PASS**, 11.225 seconds; raw log SHA256 f74316afeff1963a2f5175c794ea30d32f646e089a07b1b4da12c0c70e5d5fd3. Both operations/stats documentation checkers, bash -n for all five scripts/*.sh files, and git diff --check passed. Earlier logs remain preserved. These are lightweight checks only; hosted namespace/Go execution remains pending.
