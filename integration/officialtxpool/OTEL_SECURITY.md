# Test-only OTel logging dependency update

This update moves the integration modfile to OTel 1.45.0 and its required
dependency minimums. The root modfile, production source and Prysm source are
unchanged. Local dependency resolution, the real old-version negative control,
the new full tagged race suite and a separate non-race vet check have completed.
**As of the 2026-11-11 local verification record, the complete updated test-graph
vulnerability scan had not completed, and CI for this candidate had not been
run. This is not production release approval.**

[GO-2026-6505](https://vuln.go.dev/ID/GO-2026-6505.json) is UNREVIEWED and supplies
no affected symbols. The old integration test graph selected affected intervals
for otlptrace/otlptracehttp 1.43.0 and sdk 1.44.0. The inspected production binary
did not link these modules. This is not evidence that a real production endpoint
or credential was leaked. The upstream logging changes are in
[OpenTelemetry commit 3a1412d](https://github.com/open-telemetry/opentelemetry-go/commit/3a1412d2b3bc4e4231fbeac2ed42117ae541bb38).

## Local verification, 2026-11-11

These results were observed on linux/amd64 with Go 1.26.11, `CGO_ENABLED=1` and
`GOMAXPROCS=2`. Timings describe this local run, not a cross-platform guarantee
or service-level objective.

- **Dependency resolution:** official Go 1.26.11 resolved the actual tagged
  compiled/test package metadata. Its 166 non-main module paths match the prior
  baseline; exactly the 11 intended versions differ, with no replacement
  identity changes. Explicit queries confirmed the upgraded versions and the
  unchanged security pins. Requirement-graph edges are not an all-selected
  package SBOM. The unused authenticated sdk/metric and metric/x inputs were
  not forced into requirements.
- **Old negative control:** the same regression source, using the old integration
  modfile, exited 1 because real JSON logs exposed the synthetic endpoint and
  violated all three intended safe-shape assertions. The three positive log
  markers were present. Compile failures, panics, races and timeouts were not
  accepted as the expected failure. This run named only the regression file,
  creating `command-line-arguments`; it does not validate the old complete relay
  package or build-tag selection. Explicit file mode ignores that file's own
  build constraints.
- **New full tagged race suite:** exited 0. All 84 top-level tests and the existing
  fuzz-seed entry ran: 82 passed and the three existing opt-in checks skipped.
  Including subtests, there were 445 passes and 3 skips. Both the official BSC
  pool fixture and the new logging regression passed. Package test time was
  44.759 seconds in that run, including the official pool's 35-second cooldown.
- **Non-race vet:** a separate invocation exited 0 with no diagnostics. Two earlier
  bounded vet attempts timed out and remain incomplete results, not passes.
  Non-race vet may need different compilation/cache entries from a race build.
- **Still open at this record:** a complete vulnerability scan of the updated
  actual test graph had not completed, and CI for this candidate had not been
  run. Earlier baseline CI or an incomplete scanner pass does not satisfy these
  checks. The unchanged default-runtime suite was not
  rerun as part of this integration-only update.

The three opt-in skips were `TestSoakMsgPipe`, `TestUniqueCapacityDefaultTTL` and
`TestUniqueCapacityFailureReport`, with their existing reasons unchanged. No
soak, benchmark or fuzz campaign is implied by the normal seed execution.

## Reproduce from the repository root

Use Go 1.26.11 on linux/amd64, with a working C compiler and dependencies
matching the integration go.sum. Set the environment before running either
control or the upgraded checks:

```sh
export GOWORK=off GOTOOLCHAIN=local CGO_ENABLED=1 GOMAXPROCS=2
```

To run the old negative control against the same current regression source:

```sh
old_mod_dir=$(mktemp -d)
git show 9adb1d587ec390ee0aab63920e8e8257a2777feb:integration/officialtxpool/go.mod > "$old_mod_dir/go.mod"
git show 9adb1d587ec390ee0aab63920e8e8257a2777feb:integration/officialtxpool/go.sum > "$old_mod_dir/go.sum"
GOFLAGS='-buildvcs=false -p=1' go test -mod=readonly \
  -modfile="$old_mod_dir/go.mod" -tags=officialtxpool -race -vet=off \
  -json -count=1 -run='^TestOTelEndpointLogRedaction$' -timeout=45s \
  internal/relay/otel_log_security_test.go
```

Expected exit 1 alone is insufficient: require the synthetic endpoint leakage,
all three unsafe-shape failures and the positive client/exporter/provider logs.
Do not count a build error, timeout or unrelated failure as the negative control.
The old command uses only the explicitly named file, not the full relay package.

Run the upgraded full package and non-race vet separately:

```sh
GOFLAGS='-buildvcs=false -p=1' go test -mod=readonly \
  -modfile=integration/officialtxpool/go.mod -tags=officialtxpool \
  -race -vet=off -json -count=1 -timeout=180s ./internal/relay
GOFLAGS='-buildvcs=false -p=1' go vet -mod=readonly \
  -modfile=integration/officialtxpool/go.mod -tags=officialtxpool ./internal/relay
```

The test timeout applies to the test binary, not dependency compilation.

## Persistent regression

`TestOTelEndpointLogRedaction` is guarded by `officialtxpool` in `internal/relay`,
so the existing tagged relay CI command includes it. Its child is the current
test binary, not an extra Go command, isolating the global OTel logger from the
other relay tests. It removes inherited OTEL settings and captures upstream
funcr JSON logs without redaction hooks or field filtering.

The test constructs an HTTP client and an **unstarted** exporter using only
`otel-log-sentinel.invalid:4318` and a synthetic header sentinel. It requires
real client/exporter log records and the SDK's own `TracerProvider created`
record, rejects either sentinel in every captured line, and checks that the
client exposes only its type while exporter/processor fields contain type names
rather than nested objects.

No spans, exporter/client Start, ExportSpans, collector or requests are used.
The supplied HTTP transport rejects and counts any unexpected request, which
also fails the test. WithResource still calls resource.Environment even with
resource.Empty; clearing OTEL_* in the child environment prevents inherited
OTel resource attributes and configuration from entering this test.
The full upgraded package retains the separate official-pool loopback fixture.
A package-level focused `-run` still compiles the complete tagged relay package;
the explicit single-file negative control above deliberately has narrower scope.

## Checksums, security pins and limits

All 13 new module input versions have archive and go.mod h1 values matched by
one isolated official Go 1.26.11 download verification. The client preserved
13 SumDB lookups, 28 tiles and its checkpoint. This establishes the client's
checksum/inclusion/consistency checks for that run, not global gossip or
continuity with an earlier saved view. Registry `.info` metadata is not covered
by those two h1 values. The integration go.sum retains its old entries and adds
26 authenticated values. An authenticated input is not necessarily selected by
MVS or used by the compiled package graph.

gRPC 1.83.2, x/net 0.62.0, x/sys 0.49.0 and x/text 0.43.0 remain fixed. Upstream
monorepo replace directives are not copied. Scanner v1.9.0's old `-test` green
result omitted 84 test-only modules and is not sufficient coverage evidence.
The updated actual test graph still needs a complete independent scan before
release; neither local tests nor the baseline scan proves that result.

Prysm's own enabled `Setup` path logs its endpoint with Infof. This OTel update
does not repair that separate statement. No Setup call was found in the current
fixture; that observation does not prove the path can never be called.

## Licenses

No upstream implementation is copied into this regression. Full upstream
license files remain in the reviewed module archives and must be preserved in
any distribution requiring them. The eight OTel module LICENSE files contain
Apache-2.0 **and the appended Go Authors BSD-3-Clause notice**, not Apache alone.
logr, proto/otlp and both genproto modules retain their Apache-2.0 texts.
grpc-gateway retains its BSD-3-Clause LICENSE and the Go Authors BSD notice in
`internal/casing/LICENSE.md`. The 12 available old archives had identical
notice inventories and bytes, including that nested notice. The old metric/x
0.66.0 archive was unavailable locally, so no old/new byte comparison is claimed
for it. No standalone NOTICE filename was present in the 13 new archives.
This inventory is not an exhaustive binary-distribution compliance audit.
