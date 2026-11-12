# Experimental scanner synthetic regressions

This is a patch-only research candidate, **not a replacement for the project's
production scanner** and not evidence of a clean real vulnerability scan.
The existing three CI jobs and scanner commands are unchanged.

Upstream is Go's `golang/vuln` at
[`e672cbe8fbc8c76e28fcb6db0efd9ce354e0dac5`](https://github.com/golang/vuln/tree/e672cbe8fbc8c76e28fcb6db0efd9ce354e0dac5)
(v1.9.0). `UPSTREAM.json` records ten selected reference files, not a full license
or source audit. `MANIFEST.json` authenticates the patches, source index, license notices and this README; the Git commit binds the manifest itself.

## Scope and current verification status

The first four ordered patches propose callgraph error propagation, package-instance
traversal/original roots, exact types/SSA ownership and source-module conflict
checks, followed by real-loaded test-only symbol controls and derivative labels.
The experimental identity is `bsc-relay-testgraph-experimental`, version
`v0.1.0-testgraph.4+upstream.e672cbe8fbc8`; no official endorsement is implied.

The positive synthetic control must produce an ordered test -> wrapper -> target
witness with the fake target module/version. The ordinary-build control rejects
target imported/symbol results; it does not independently assert absence of all
module-level emissions. No synthetic entry is a real vulnerability advisory.

At candidate preparation time, these Go changes have **not been compiled or
executed**. Source-level review and sequential patch application are not runtime
verification. Old-red/new-green, full upstream compatibility, actual relay graph
coverage, current real advisory coverage, dependency-license completeness and
production acceptance remain open. No scanner binary or complete fork is shipped.

## Local candidate checks (2026-11-12)

The 57 Python tests (46 existing plus 11 new pure/mock contracts), two existing
documentation checkers and YAML parsing passed locally. These tests did not run
Go, module downloads, namespace setup or candidate code. An initial new-contract
run reported one manifest error after contribution-license creation stopped on
an incorrect assumption about the upstream copyright line; the copy was corrected
against the actual unmodified notice and the full suite was rerun successfully.

Whitespace checking applies to the harness, tests, workflow and prose. The five
hash-pinned `.patch` payloads intentionally contain unified-diff context prefixes
(space-only context lines and space-before-tab prefixes); a raw Git whitespace
check flags these as data, so no whole-patch whitespace-clean claim is made.
Payload integrity is instead checked by SHA-256 and sequential hunk review.

## CI isolation contract

The separate experimental job checks out the exact official upstream revision,
verifies the reference and patch hashes, and downloads only its ordinary pinned
Go modules. This preparation does not execute candidate code. It uses official
Go 1.26.11 `gofmt` on the ten changed Go files without `-s`, prints the formatting
diff, and records both pre-format and actual normalized source hashes. Thus the
executed source is a documented formatting transformation, **not a claim that
unformatted patch bytes were tested unchanged**.

The test phase uses a disposable Linux network and PID/proc namespace, an outer
360-second watchdog, and an ordinary uid/gid with no supplementary groups,
capabilities or privilege escalation. A clean environment disables toolchain
switching, telemetry and online module resolution. It verifies a different
network namespace, no routes and only the down loopback interface before Go runs.
No host networking, firewall or service configuration is changed. Missing tools,
privileges, isolation or dependencies fail closed; there is no online fallback.
The namespace lifetime also contains child processes. These are designed guards,
not a claim of a previously executed containment or hostile-code audit.

Only eleven explicitly enumerated new tests in two packages are selected. They
use generated source and in-memory synthetic OSV data. Any skipped test, missing
selected PASS, failed package, excessive output or source drift is a failure. A started attempt cannot be silently reused, including after failure; use a fresh prepared state and retain the earlier logs.
The workflow does not invoke the real scanner, real vulnerability database,
public-chain nodes, full upstream test suite or real relay graph scan. Ordinary
module download is not permission to fetch previously blocked vulnerability
content through another route. `GOPROXY=off` alone is not the network boundary.

The first hosted run still must validate Linux runner compatibility, formatter
results, compilation, fixture behavior and these isolation guards. A green toy
regression does not close the known real-project coverage or production gates.

## Licensing

This directory's Go-derived patch context and explicitly BSD-marked new scanner
contributions are a **BSD-3-Clause exception** to the main relay's GPL-3.0-or-later
source license. Original Go Authors notices remain in the patch context.
`LICENSE` and `PATENTS` are copied verbatim from the pinned upstream;
`LICENSE.CONTRIBUTIONS` supplies the complete BSD-3-Clause terms for newly
written scanner contributions. The relay's Python harness remains GPL-3.0-or-later.
See the root third-party notice for the exact modified upstream copyright years.
This patch-only review is not a complete scanner/dependency/binary distribution
license audit, and does not permit redistribution of an unrelated Prysm fork.

## Static fixture environment correction

Review of the pinned packagestest implementation found that generated fixtures
inherit `GOFLAGS` without removing `-mod=readonly`. The harness now applies
`-mod=readonly` only to the outer `go test` command, not the inherited environment,
so generated fixture modules may write their own sums with their local file proxy.
Network namespace isolation and upstream source/change-scope checks, including
rejection of changes to upstream `go.mod` or `go.sum`, remain unchanged.
This corrects a statically identified configuration risk, not an observed Go test
failure: no Go, namespace, download or hosted test has been executed locally.
The cold preparation and isolation time budgets remain unvalidated.

## Static generic fixture correction

The fifth ordered patch pins only the generated generic fixture's language to
Go 1.18 after packagestest Export/Finalize, because Finalize overwrites a staged
`go.mod` and does not itself supply a language directive. It verifies the fixture
module before writing and verifies the loaded module's path, GoVersion and GoMod
path afterward. The tool's own `go.mod`/`go.sum` are not changed. This is a static
configuration-risk correction, not a previously observed Go execution failure.
The first four patches and the eleven selected test names are unchanged; the
experimental identity remains the fourth patch's identity, with the Git commit
and five-patch manifest binding this fixture-only follow-up.

After both static fixture corrections, 59 Python tests (46 existing and 13 new
pure/mock contracts), both documentation checkers, YAML parsing and non-patch
whitespace checks passed. Go compilation, formatting, module downloads,
namespace execution and hosted CI remain unexecuted by this preparation step.

## First hosted formatting failure and correction

Candidate `b53d91b2`, run `43238076103`, job `131250739367` stopped at
`gofmt -d` exit 1 in preparation, before formatting writes, compilation or the
network namespace. Go 1.26.11 intentionally reports formatting differences as
exit 1: see [the pinned formatter source](https://github.com/golang/go/blob/go1.26.11/src/cmd/gofmt/gofmt.go)
(blob `ad6ad636524479053b5609a023c1a69a03f378e8`). The original failure is retained.
The harness now saves all original bytes and hashes, requires `gofmt -w` exit 0
without `-s`, then requires successful empty `gofmt -l`. Python generates the
recorded diff from actual before/after bytes. Every nonzero command still aborts;
no blanket exit-1 exception or retry conceals a syntax/write failure. A preparation
receipt is written only after normalization succeeds. Local checks are Python
and documentation checks only, not a local Go or namespace execution.

The formatter correction passed 62 Python tests (46 existing and 16 new
pure/mock contracts), both documentation checkers, YAML parsing and non-patch
whitespace checks. The exact diff bytes are also printed with strict UTF-8
decoding for the CI log; a captured-output regression matches stdout to
`format.diff` and checks the exact before/after hashes. These are local mock
checks, not successful Go formatting or hosted execution evidence.
