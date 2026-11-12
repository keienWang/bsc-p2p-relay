# Stage 0: fixed-input BLS declaration inventory

Status: candidate metadata collector; no actual Go result, race result, license
approval, production fix, or complete native compilation inventory is asserted.

## Scope

The fixture is stored as research/bls-stage0/probe_test.go.txt. It has only a
blank test import of Prysm crypto/bls/blst. No Go source package is added to the
ordinary repository test graph. This is NOT the exact graph of the six black-box
or ten total regression tests in the separate frozen patch bundle. Those tests
remain unrun and unchanged. A successor public-key-only fixture and later
dynamic suite must collect their own exact graphs. No -race build flag is used
in this metadata probe; a race-instrumented job must recheck its exact graph.

The existing five CI jobs and source/go.mod/go.sum remain unchanged. A sixth
research job collects only the probe metadata described here.
This collector does not apply the cache patch, build, test, vet, scan, sign,
submit transactions, access accounts, or run a service.

## Fixed inputs and isolated preparation

Run from the independently reviewed candidate checkout. Its publicly resolvable
baseline is 560ddfc85e05f9a22d16d3b420396d10605e00c8; the parent-confirmed
source-equivalent local baseline is df604e0453d580217071cd189e8cf1e5054aa196.
These are not identical Git identities. Exact input hashes below/in the
collector are the authoritative input binding.

The collector verifies its fixed root LICENSE, go.mod/go.sum and every existing
compat/stunv2 file, then copies those exact bytes and the probe into a disposable
module work tree. The main module remains github.com/keienWang/bsc-p2p-relay.
This deliberately uses relay's locked module graph, NOT the official Prysm
checkout as the main module (which otherwise selects a different BLST version).

Only these command families are invoked, with a four-minute per-command cap and ten-minute metadata-work deadline.
Failure cleanup makes bounded TERM/KILL reap attempts of 0.5 and 1.0 seconds;
there is no unbounded Popen context-manager exit. The 12-minute outer CI limit
and disposable runner remain the final cleanup boundary.
Each command has a fresh process group; deadline/output failure uses TERM then
KILL for that group, not only its leader:
1. go env -json for explicitly named tool/environment fields.
2. go mod download -json for exactly Prysm v5.3.2, BLST
   v0.3.17-0.20260401165437-a3c15d6a6c68, and Hashicorp LRU
   v0.5.5-0.20210104140557-80c98217689d.
3. go list -deps -test -json ./internal/blsgraphprobe.
4. go list -m -json for the selected package modules plus the three fixed pins.

The collector creates private temporary module/build caches, HOME and TMPDIR.
The actual paths returned by go env are checked against these controlled paths.
Its output must contain exactly one object with precisely the requested keys.
The resolved Go binary must be a regular, bounded-size file; its SHA is checked
before and after collection. GOROOT must match that installation and its VERSION
file must identify Go 1.26.11 without changing. This is not complete toolchain
attestation. Individual files are capped at 32 MiB, selected-file/license reads
at 128 MiB total, selected files at 4096, and safe output at 4 MiB.
Go 1.26.11, Linux/amd64 host and target, GOAMD64=v1, CGO=1, GOMAXPROCS=2, and
GOFLAGS='-mod=readonly -buildvcs=false -p=1' are mandatory. No workspace,
alternative modfile, overlay, external tags/flags, toolchain auto-download,
private-module credentials or caller-supplied module proxy is accepted.

Ordinary module preparation uses only https://proxy.golang.org without a direct
or alternate fallback. GOSUMDB=off avoids additional database access; every used
module Sum/GoModSum must instead match the already committed, hashed go.sum.
There is no vulnerability database access. go mod download can fail if it needs
different lock contents: no mutation is accepted. No tidy is run. The three pins
and Prysm's two original file hashes are independently checked after preparation.
Other transitive sources needed by go list may be fetched from that same proxy
using the frozen root graph; this is not go mod download all.

All copied inputs and original repository inputs are rechecked afterward.
Temporary source copies and caches are removed before a successful report is
returned, and also on ordinary failure. An external runner kill may prevent
Python cleanup; CI must use a disposable runner and never export its caches.

## Output and failure contract

CLI: no arguments. Unknown arguments or build-affecting environment input fail.
On success stdout contains only the safe JSON inventory. On failure stdout is
empty and stderr contains one stable BLS_STAGE0_ERROR code with optional tool-output hashes/byte counts, never raw go-list
JSON, compiler/tool output, source, credentials or absolute paths.

Package instance IDs, ForTest, Imports and ImportMap are preserved, including
the probe's ordinary package, its [probe.test] instance and testmain. They are
not collapsed by import path. Non-probe upstream test instances are rejected.
Generated testmain source is represented separately: if go list materializes it
inside the controlled temporary state its bytes are hashed; otherwise the
limited known generated source is explicitly marked unmaterialized with no SHA.
No missing static file is silently dropped.

For selected package declaration files the report contains component-relative
names, source-field roles and SHA-256 values. Module original/effective identity
is explicit. This initial collector does not support effective module replacements;
if one is selected it stops with validated original/replacement module coordinates
(or a redacted local-path marker), so its scope can be reviewed. The copied root
BSC/Comet/STUN replacements are preserved. Whether they enter the direct-blst graph
is unverified, not assumed unrelated.
Only the probe's own TestGoFiles are treated as probe declarations; dependency
test declarations are not represented as selected upstream tests.

License/NOTICE/COPYING files found in package ancestors and component roots are
listed by relative name and SHA only. Finding a license does not determine its
legal applicability or completeness. An empty list is not approval.
Known build/bazel and SSZ equality package presence is reported for review.
The bls wrapper/Herumi packages are rejected as outside this direct-blst scope.

GoFiles/CgoFiles/CFiles/SFiles and related fields are a SELECTED GO PACKAGE /
DECLARATION inventory. They are not the complete C-preprocessed native include
closure. The BLST module h1 is fixed, and bindings/blst.h, src/server.c and
build/assembly.S have separate reference hashes. Those are reference files,
not a claim that all their contents are selected by the compiler.

## Candidate CI integration

The `experimental BLS inputs only` job uses the existing pinned checkout/setup-go
actions, Go 1.26.11, `persist-credentials:false` and explicitly `cache:false`.
There is no actions/cache or artifact upload. Its 12-minute outer limit encloses
the collector's ten-minute metadata budget plus bounded process reaping and
runner cleanup. The collector step has an independent 11-minute step limit.
The only network preparation is the fixed ordinary module proxy route described
above; the job performs no new vulnerability-database request.

The collector runs with no CLI arguments and redirects only its safe JSON stdout
to a runner-temporary file. A separate step checks the expected schema and scope,
then logs its byte count, SHA-256 and safe JSON. No raw go-list JSON, source tree,
module/build cache, binary, native archive or crash dump is exported. The existing
five jobs keep their original bytes and validation scopes; their cache policies
are not being described as this new job's policy.

This workflow addition still needs independent review and actual hosted execution.
Neither a local Python pass nor older CI proves that this new job succeeds.

Current verification is Python-only mocks and static input checks. A successful
future stage0 merely proves collection and input matching for this probe.
It does not prove license closure, test execution, baseline race reproduction,
candidate race freedom, consumer coverage, native source selection, performance
or RSS bounds. Those remain separate gates.

## Local integration verification (not hosted execution)

On 2026-11-12, the collector's 23 Python contracts passed, including real Python
subprocess timeout, output-cap and descendant process-group cleanup checks.
The integrated suite passed all 166 Python tests in 16.351 seconds; both existing
documentation checkers, YAML parsing, the new job's Bash syntax and scoped diff
checks also passed. No Go compiler, Go package, BLS code or network ran locally.

The first integrated Python attempt failed two existing workflow-shape assertions:
one expected exactly five jobs, and another treated the scanner job's entire
remaining file suffix as one job. The correction explicitly expects the added
sixth job and bounds both scanner assertions to their original section. No
scanner test assertion was removed. A new contract hashes the original five-job
prefix and checks the new job's no-export/no-test and time-limit settings. The
original failing result is retained separately from the successful corrected run.

The successful local unittest output SHA-256 is
`5dd2d03d44e112f322afb04dbe4de77a241c81d856fb6595ed30aaba50b5f9e0`.
This is local integration evidence, not an actual inventory or a hosted CI pass.

Independent review then requested two additional failure checks: exact one-object
`go env` output with precisely the requested keys, and explicit `GOAMD64=v1`
rejection/query/receipt binding. With those guards and negative cases added, the
final full local suite passed **168 Python tests in 18.798 seconds**; both docs
checkers and scoped diff checks passed again. Final unittest log SHA-256:
`bc194b74fc9d67244f9548640e14760a86936b75dcd9808442f0741e7e939cd8`.
The earlier failed and successful attempts are retained as separate evidence.
This final check still executed no Go, native code, BLS test or network request.
