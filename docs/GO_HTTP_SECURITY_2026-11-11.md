# Go HTTP security patch, 2026-11-11

## Trigger and scope

[Workflow 42967328641](https://github.com/keienWang/bsc-p2p-relay/actions/runs/42967328641),
for public commit `b2f12a959b65717461062366ca9ed72f78750ca8`, passed the functional
and race steps in all three dependency graphs, then failed each mandatory scanner
with one called standard-library finding: [GO-2026-6859 / CVE-2026-97054](https://pkg.go.dev/vuln/GO-2026-6859).
The previous successful scan records remain historical evidence, not current
clearance. No failed run is erased or converted to success.

The official [advisory JSON](https://vuln.go.dev/ID/GO-2026-6859.json), retrieved
2026-11-11 UTC, was published at 02:51:11Z that day. It describes an HTTP/1
MaxBytesReader/MaxBytesHandler race when a handler reads a request body concurrently
with writing response headers. Exceeding the body limit could mutate the header
map from the body-reading goroutine and crash the server.

The relay uses MaxBytesReader in `internal/relay/http.go` for transaction requests.
Its current handler decodes the body and writes the response on the same goroutine;
the API also binds only to loopback. No exploit of this exact handler was proved.
These observations do not waive the finding or justify retaining the vulnerable
standard library.

## Minimal fix and provenance

[Go's release history](https://go.dev/doc/devel/release#go1.26.11) identifies
Go 1.26.11 as a stable patch released on 2026-11-10. The advisory lists fixes in
1.26.11 and 1.27.4; 1.27.0 through 1.27.3 remain affected. A `go` directive is a
minimum version, not proof that every later version is security-patched.

The three CI jobs and all three module minimums now use 1.26.11. CI disables
automatic toolchain switching and explicitly checks `go env GOVERSION` before
building. Existing scanner commands, tags, dependency boundaries, and failure
semantics are retained. There are no module dependency, go.sum, historical SBOM,
Prysm, or production handler changes.

Official Linux amd64 artifact:

- URL: https://go.dev/dl/go1.26.11.linux-amd64.tar.gz
- Manifest: https://go.dev/dl/?mode=json&include=all
- Archive bytes: 66999944
- Archive SHA-256: `9849f4e04ee35303714e34fbc3ac8ab44ea3e448a95aa182f91e3b9b79c710ec`
- Extracted `bin/go` SHA-256: `95499375c24b610cdc9e3684cec920174ec4c4f3f66d35b4332107f0f7d3d27d`
- Verified identity: `go version go1.26.11 linux/amd64`

[Upstream fix CL 862185](https://go.dev/cl/862185), merged as
`705bd8c632adcb9efbc8253e15d53413c7edcd9a`, makes the body-limit flag atomic and
moves connection-close/header decisions out of the body-reading goroutine.
The checksum-verified distribution includes this change and upstream
`TestServerMaxBytesHeaderRace` in `net/http/serve_test.go`. The full patch release
also contains other Go fixes; its entire server.go diff is not attributed to this
one advisory.

The added relay regression uses real loopback HTTP/1 and an oversized JSON body,
checks the unchanged 400 response and connection-close response semantics, rejects before Submit,
checks HTTP slot recovery, and confirms a later health request succeeds. It is
not presented as a reproducer of the concurrent upstream race, and does not by
itself distinguish patched from vulnerable toolchains. It checks response.Close,
not a direct observation of TCP EOF; no old-toolchain control run was performed.

## Local validation and limits

All executed source checks used commit
`08f350c6fd07fe18ed2bed6c0ad20031855e8984`, tree
`91f828846fb8fa14f180ad649ed4f0dbb4c34ae1`, on Linux amd64 with CGO enabled.
The subsequent documentation-only commit does not change this tested code.
The official Go 1.26.11 binary was explicitly selected on PATH, with
`GOTOOLCHAIN=local`, `GOWORK=off`, `GOENV=off`, `GOMAXPROCS=2`,
`GOFLAGS='-buildvcs=false -mod=readonly -p=1'`, pre-cached modules, and
`GOPROXY=off`. Each bounded stage ran serially under one shared flock. Original
intent, identity, stdout/stderr and exit records were retained, including failures.

| Stage on 2026-11-11 UTC | Result |
| --- | --- |
| `go test -race -count=1 -timeout=90s -run='^TestServerMaxBytesHeaderRace$' -v net/http` | Exit 0; actual h1 and h2 subtests passed; package 1.043s. |
| `make check smoke TEST_TIMEOUT=300s`, 600-second outer stage limit | **Exit 124 at the limit**, during `go mod verify`. Vet, all five default race-test packages, build, 43 Python tests and formatting had already passed. Dependency guard and smoke had not run. |
| Separately authorized `go mod verify`, 115s TERM + 5s grace | Exit 0; all modules verified. |
| Separately authorized `python3 scripts/check_runtime_dependencies.py`, 55s + 5s | Exit 0; dependency guard passed. |
| Separately authorized `python3 scripts/smoke.py bin/bsc-relay`, 55s + 5s | Exit 0; actual process startup, health, readiness, stats identity/schema/headers/accounting, malformed input, shutdown and no-data-files checks passed. |

The successful default race package times were checkpointproof 40.400s,
compatcheck 1.142s, evidence 1.082s, firstepoch 41.852s, and relay 6.348s.
The relay package includes the new real HTTP/1 body-limit regression. Vet, race,
build and Python tests were not rerun to replace the timeout. This is a completed
set of **segmented checks**, not a successful single `make check smoke` invocation.
The smoke-tested binary reports Go 1.26.11 and has SHA-256
`330f4a842e2f47f31c0fbf12ffe7a88ec1cf77702e82a68a9fa032a84d1d4103`.

### Three exact dependency-graph scans

Local scanning reused the already verified govulncheck v1.9.0 binary, whose own
build toolchain is Go 1.26.10. Its SHA-256 is
`0108fc96c31b697274b66fc071da4ccc9f81de4079bb016867e3a479796d444c`.
That is the scanner's identity, not the target's Go version. `GOVERSION`, `GOROOT`
and `GOPACKAGESDRIVER` overrides were cleared; fixed PATH and actual `go env`
values were recorded. All three full JSON streams independently identify:

- config.go_version and SBOM.go_version: `go1.26.11`;
- SBOM standard-library module: `stdlib@v1.26.11`;
- database: `https://vuln.go.dev`, modified `2026-11-11T02:51:11Z`;
- the complete GO-2026-6859 advisory with the fixed 1.26.11 range;
- no findings, including no GO-2026-6859 finding.

Each source scan had to exit 0, parse completely, and pass these identity/data
checks before the same scanner converted its full JSON with
`govulncheck -mode=convert -show=verbose`. JSON mode alone is not a vulnerability
gate: it can exit 0 even with findings. The conversion's real exit code was
preserved. All final text gates exited 0 and reported no vulnerabilities.

The commands below used the common environment above and the official default
database. Each source scan had a 115s TERM + 5s grace limit. Identity checking used
isolated Python (`python3 -I`) with a 4s + 1s limit.

| Graph | Additional GOFLAGS | Source command | Final gate |
| --- | --- | --- | --- |
| Default | none | `govulncheck -json ./...` | Source/identity/convert each exit 0. |
| Official txpool test graph | `-modfile=integration/officialtxpool/go.mod -tags=officialtxpool` | `govulncheck -json -test -tags=officialtxpool ./internal/relay` | Source/identity exit 0. First conversion timed out, then a separately authorized conversion of the **same checksum-verified JSON** exited 0. |
| Canonical isolation test graph | `-tags=canonicalisolation` | `govulncheck -json -test -tags=canonicalisolation ./internal/canonicalisolation/...` | Source/identity/convert each exit 0. |

The first official conversion reached its 10-second TERM limit and exited 124
with empty text output. Its reason was not established; this was neither a new
vulnerability finding nor a completed gate. That record remains unchanged. The
window stopped immediately and did not attempt the canonical scan. A separately
authorized window converted the original official JSON with a 55s + 5s limit,
then ran the canonical scan once and converted it with the same longer limit.
Neither default nor official source scanning was repeated. Both completed
conversions retain the same v1.9.0 scanner and official database configuration.

### Retained evidence identities

SHA-256 values of the retained raw records:

| Record | SHA-256 |
| --- | --- |
| Upstream HTTP race output | `631ace1d1e4716b250fc87d040eeb282d4e0123e4e5e7c6aa4531eacaee1204d` |
| Original default aggregate output, including timeout | `50e417a0e7ec945f51191452e4b3000efbd9318d94ffe9fa782d1c793b5a6a40` |
| Actual smoke output | `f4e365d38abead7b4e0b66e4fe9b96c7cd06632ec7447af9b8b790991611d747` |
| Default scan JSON | `23099217d0e46d48c202072e7062bdbd97a5922950a185e01352805c0113daf5` |
| Official scan JSON, used unchanged for both conversions | `4f12e955b3e40096d2eab908838011a9ad0dcad96d78d8a79ed92f46105afd8f` |
| Canonical scan JSON | `7b65f5548fbbe2360abe5a3498fa651e1f8860b7e6c71538ccbc8b462e139711` |

The two opt-in tagged functional/race suites were **not rerun locally on Go
1.26.11** in this patch validation. Their older Go 1.26.10 successes in workflow
42967328641 are historical only. Publication and all three complete jobs on the
exact new public commit, including fresh-Go tagged functional/race checks and
mandatory scanners, remain required. No scanner was suppressed or weakened.

These results do not establish production readiness or complete public-network
validation, independent audit, the broader production checklist, or the separate
canonical/Prysm research. No real transactions, public BSC/RPC services, or
unapproved servers were used.
