# Submission statistics: local verification record

Date: 2026-11-10 UTC. This is local verification, not hosted-CI confirmation,
public-network delivery, a release artifact, resolution of the BLS concurrency
gate or production certification. Earlier records and measurements are unchanged.

## Source and scope

- Runtime/API implementation: `f67a508c8e03e257c60096b65eba05f2dd450f44`.
- Reviewed implementation plus the requested regression additions, **full-scope tested source**:
  `0c8bdd864adcdd65fa74cdfd28922878eec3177c`, tree
  `d6a5a8f48c398d0400ada5b511660398a2de6053`.
- Subsequent process-smoke-only test change:
  `7b56311f6ae4b29e1915afe5399aab1d552cda3e`, tree
  `64bce3f3e57876c204bb905d65a673d2411fab29`. This changes only
  `scripts/smoke.py` and `scripts/test_smoke_stats.py`; runtime, Go tests,
  dependencies, CI and SBOM remain identical to the full-scope tested source.
- This verification-record successor changes documentation only. Do not describe
  a documentation-only successor as a separately rerun expensive test suite.

The [frozen stats contract](OBSERVABILITY.md) has eight normal-return Submit
outcomes and one HTTP concurrency-refusal counter. Tests cover unchanged old JSON,
random instance identity shared across Handlers, exact distinct field mapping,
uint64 values above 2^53 and at MaxUint64, validation/no-peer/cache/queue/expiry
outcomes, partial fanout, rollback, cached retry and TTL, panic/unfinished calls,
lock-free snapshots while all three state locks are held, per-Handler slot
isolation, and a refused response whose JSON write fails without undoing the
refusal decision or adding a Submit outcome.

## Environment and isolation

Linux amd64, Go 1.26.10, `GOMAXPROCS=2`, `GOENV=off`, `GOWORK=off`,
`GOTOOLCHAIN=local`, `GOPROXY=off`, `GOSUMDB=off`, pinned local module/build caches,
and `-p=1`. One shared load lock serialized the workload; each phase had its own
finite outer deadline, exact source/command/environment intent, output log and
exit record. No long soak, 150-second retention experiment or benchmark was run.
No public peers, real signed transactions, external RPC or private project inputs
were used. Security scans are separate from P2P network testing.

The independently built scanner's buildinfo identifies
`golang.org/x/vuln/cmd/govulncheck` v1.9.0, built with Go 1.26.10. Its binary SHA-256:

`0108fc96c31b697274b66fc071da4ccc9f81de4079bb016867e3a479796d444c`

It was not rebuilt under officialpool or canonical tags. Actual successful scans
of the default production graph, default test graph, actual officialpool test
graph and exact canonical-tag test graph each reported **No vulnerabilities
found**. These are results at the recorded scan time, not a future guarantee.

## Commands and graph separation

The following scopes normalize only the machine-local scanner and fresh-fixture
paths. The evidence retains exact argv and environment; no machine-private paths
are needed to reproduce the repository scopes.

- Default `GOFLAGS=-buildvcs=false -mod=readonly -p=1`:
  `make check`, `make smoke`, `make fuzz` (the existing 10-second, two-worker
  target), attempted `make vuln`, then the approved standalone v1.9.0
  `govulncheck ./...` and `govulncheck -test -show=verbose ./...`.
- Officialpool adds exactly
  `-modfile=integration/officialtxpool/go.mod -tags=officialtxpool`:
  `python3 scripts/check_runtime_dependencies.py`, `go vet ./internal/relay`,
  `go test -race -count=1 -timeout=180s ./internal/relay`, and
  `govulncheck -test -tags=officialtxpool -show=verbose ./internal/relay`.
- Canonical removes the official modfile/tag and adds only
  `-tags=canonicalisolation` to the default flags. Fresh fixture generation uses
  `go test -count=1 -timeout=180s -run='^TestModernWireCheckpointRoundTrip$' -v ./internal/checkpointproof`
  with `BSC_MODERN_WIRE_OUTPUT_DIR` naming a **nonexistent** new target. The test
  exclusively creates that target. Then run the dependency guard,
  `go vet ./internal/canonicalisolation/...`,
  `go test -race -ldflags='-s=false -w=false' -count=1 -timeout=180s -v ./internal/canonicalisolation/...`
  with `BSC_CANONICAL_FIXTURE_DIR` referencing that exact fresh output, and
  `govulncheck -test -tags=canonicalisolation -show=verbose ./internal/canonicalisolation/...`.
- Every phase starts without inherited `RELAY_SOAK*`, `RELAY_UNIQUE*` or `BSC_*`
  fixture variables; only that phase's explicit fixture path is supplied.

The successful fresh fixture has 3,074 synthetic headers. Its `headers.rlp`
SHA-256 is `08d59559eb1dce022f15f345cd858150ec5ce1770b36155cf5604b1291fdea5a`; `manifest.json`
SHA-256 is `60ed8d5e1ee59f2c96b277eb4db28d9ac781be5b6ed2bc23c262651aca2a480a`. These identify
synthetic research artifacts, not real-chain or production verification evidence.

## Outcomes, including unsuccessful attempts

The initial `make check` passed default vet, all-package race, build, 31 Python
regressions, formatting, module verification and the runtime dependency guard.
The original process smoke passed. The officialpool and canonical graph-specific
guard/vet/race/scans later passed on the same frozen source. No production gate
was waived by these local results.

Three unsuccessful preparation/launch attempts remain part of the evidence:

1. **03 fuzz:** the 90-second outer limit killed the command after its log showed
   18/18 baseline completion and entry into two-worker fuzzing. It has no PASS
   and is not a completed fuzz run. That log has no line timestamps, so the
   presumed preparation delay is not a measured conclusion. The explicitly
   authorized **03b** changed only the outer budget to 300 seconds, keeping
   `make fuzz`, 10 seconds, two workers, source and assertions unchanged. It
   passed: 889 executions, Go-reported 11.078 seconds, about 16.71 seconds total.
   Automatic Go fuzz-cache growth changed its baseline to 23 entries; no test
   input or assertion was manually changed. Retry logs record UTC and monotonic
   **line receipt** times, not inferred internal event clocks.
2. **04 make vuln:** `go run ...@v1.9.0` could not load module deprecation metadata
   with `GOPROXY=off`; the scanner never started and make exited 2. It is not a
   successful make target or a vulnerability finding. The approved **04b** used
   the verified standalone v1.9.0 binary on the identical default `./...` graph
   and flags, without changing Makefile, proxy, dependencies or assertions.
3. **10 fixture export:** the runner mistakenly pre-created the target directory;
   the existing fixture test's exclusive `os.Mkdir` rejected it. The failed log
   and original empty directory were retained. The approved **10b** used a
   different, previously nonexistent target and let the unchanged test create
   it. Fixture pins and the 180-second Go deadline were unchanged. Only after
   10b passed were the unrun canonical checks started. Never pre-create this
   fixture's target; only its parent may already exist.

Each failure stopped its batch. No previously passing default or official scope
was repeated as part of these corrections. Failed-attempt files were checked
byte-for-byte by SHA-256 after continuation rather than overwritten as passes.

## Actual binary stats smoke follow-up

After the frozen full-scope run, the separate smoke-only commit listed above
extended the real loopback/no-discovery binary exercise. One new `make smoke`
passed fixed field/type/header checks, initial zero counters, repeated stable
instance ID and creation time, and the original malformed `0x00` request causing
exactly one validation rejection with every other outcome/HTTP counter still
zero. No signed transaction was submitted. Existing health/readiness, shutdown
and no-persistent-data checks remain.

The smoke JSON checks reject duplicate/missing/unknown/null fields, unsupported
version, invalid timestamp/identity and non-uint64 counters rather than silently
substituting zero. Descriptive timestamps are not required to be ordered.
All **36 Python tests**, schema/documentation and formatting checks passed on
that smoke-only commit. Expensive Go suites were not rerun for the Python-only
change, and runtime sources were checked unchanged.

## Phase log digests

Each row identifies the retained `output.log` by phase, exit and SHA-256. Elapsed
seconds describe this verification job, not relay performance. `-9` is Python's
child-return convention for SIGKILL (the enclosing Python launcher surfaced 247).
The raw original attempts 03, 04 and 10 remain failed. All other listed rows have
exit 0. Phase-specific intent files retain exact timeout and environment values.

| Phase | Exit | Elapsed seconds | output.log SHA-256 |
| --- | ---: | ---: | --- |
| `01-make-check` | 0 | 150.595 | `4977679d02599da9a04492d97eb539dd5a366bb1643072692e4820e3f84d5bce` |
| `02-make-smoke` | 0 | 0.517 | `c43866b781185e4b85c047a3194940edb4b12c3f09a64035f7f4273901d02b89` |
| `03-make-fuzz` | -9 | 90.009 | `eb93a1dff76cd73f57649d4dd2623d77ea0c87678b8a6f327b50d6d36d37ffa8` |
| `03b-make-fuzz` | 0 | 16.724 | `b7db422601569c7db562a7e88b8c74ebb54fb4ecc9bf1ea2689f4962a0a7169b` |
| `04-make-vuln` | 2 | 0.033 | `2ef32c7d5f2a6bed7c19fa1ba9338af582dedb7dad5adc2dd2db5cf36ea50e9c` |
| `04b-standalone-default-scan` | 0 | 18.705 | `205864b93c49ea858f0998c64a04bb8abde096ef73fae1098956c77e2556d1dc` |
| `05-default-test-scan` | 0 | 11.408 | `6f1fd69d3ca980f37e9c864d1ae142f019645c6074185648f3a8f40db326aa71` |
| `06-official-guard` | 0 | 0.646 | `41d49b5203daa3c3c95784b40de9d21858df519739cc03411365aad9e3a32d15` |
| `07-official-vet` | 0 | 1.990 | `10b179d34be9aec9c98fad036652f208c54b0956470009c14bbd9efa0c794941` |
| `08-official-race` | 0 | 49.602 | `9549bdfe2a68f0e2c07305d4c0f26e2c3310a70ed985913b16d584c3a6243347` |
| `09-official-test-scan` | 0 | 15.319 | `8258e85fd7050b95e14a8044346182d82c3a8a5fc68fbfaeffff54a5d9207aaa` |
| `10-canonical-fresh-fixture` | 1 | 19.618 | `49f8409178d6114b5dd97a6e98e13f707f7092a7d1973d18ad9981bb36788ff6` |
| `10b-canonical-fresh-fixture` | 0 | 15.397 | `b12622a9218b2370f957f5a9b7d0639505f49447dc4dd21868b8657099b8f04b` |
| `11-canonical-guard` | 0 | 0.630 | `bffdacfeff296dc34e42f845d642a925129c3fe1f453b6c5d3966bf37bcad1f4` |
| `12-canonical-vet` | 0 | 0.915 | `e048e81aa9fa7ebf134de4f1a8ba9c6856c69a051cb375ae81dbb2a7cdbf549a` |
| `13-canonical-race` | 0 | 105.265 | `184b56ce6e8d1f019f757ec84ffa2c982b6194a4989b5ae8d7a7c4d25bd1b2e0` |
| `14-canonical-test-scan` | 0 | 15.394 | `e6e947e15e632222fdfaa037f0abdb51b4313ad931981a2da4261ad1ac6e3d16` |
| `15-doc-schema-format` | 0 | 0.153 | `1ba66c650aaa5546a53df7e31561d405ac168f5341b78ab9aa3a8beec9d75c9e` |
| `16-stats-process-smoke` | 0 | 0.627 | `c9fca26bd352147df85ea2bd2c1a4c486262f2d726e4279ddab2bda08958a78d` |
| `17-smoke-python-doc-format` | 0 | 0.614 | `e8bb90c0b5b7aca6cd5fb6271624c44aab8a8c3a0f0f5fcb75cefa9c8aaee0e7` |

## Deliberately not run or not established

- The known-failing opt-in `blscacherace` diagnostic was not run. Its documented
  upstream concurrency/resource issue remains an unresolved production gate;
  default/tagged passing scopes do not hide or resolve it.
- Long-duration target-hardware soak, new capacity benchmarks, public-network
  current-node acceptance and real transaction tests were not performed.
- Current authenticated head, modern verification/BLS safety, transaction
  confirmation, inclusion/execution/finality and production readiness remain
  unimplemented or unaccepted as documented in the production checklist.
- These local results are not hosted CI or publication evidence for an eventual
  remote commit. No dependency/CI/SBOM changes, external publishing or release
  deployment occurred in this work.


## Smoke framing fault follow-up

A later independent review reproduced a test-helper gap: Python
`http.client.HTTPResponse.read(amt)` can return early EOF without raising
`IncompleteRead`. A complete valid stats JSON body accompanied by a Content-Length
ten bytes too large could therefore pass the earlier bounded smoke read. This
does not undo the earlier successful real-binary responses, but those runs did
not prove rejection of that framing fault.

Test-only correction `d22aacc03f2e07bf74c9b6fe6fd92d4d2ee803c6` changes only
`scripts/smoke.py` and `scripts/test_smoke_stats.py`. It keeps the bounded
16-KiB-plus-one read and explicitly supports one nonnegative ASCII-decimal
Content-Length within the bound, with received byte count matching it, or an
EOF-delimited response without a declared length. Duplicate or comma-ambiguous
length declarations are rejected. Transfer-Encoding is explicitly unsupported in
this smoke path, including TE plus Content-Length conflicts; it is not silently
ignored. This is a conservative test helper for the supported relay response
paths, not a claim to implement a complete arbitrary HTTP parser or a change to
the product API.

Regression fixtures use actual `http.client.HTTPResponse` over a BytesIO socket
wrapper, without opening any network connection. Coverage includes the complete
valid-JSON/short-Content-Length-body reproducer, valid declared and EOF-delimited
responses, exact and over-limit bounds, invalid/negative/duplicate length values,
TE conflicts, zero-length non-observations and matching-length invalid JSON.
Invalid framing never substitutes zero counters or refreshes a stale observation.

All **43 Python tests** (the previous 36 plus seven framing tests), API/document
schema and formatting checks passed on that exact correction commit. One new
`make smoke` also passed through the actual isolated relay binary, retaining its
schema/headers/identity, initial-zero and exact validation-increment assertions.
Go runtime, Go tests, dependencies, CI and SBOM are unchanged from the full-scope
tested `0c8bdd8` source; expensive suites were not rerun for this Python-only fix.
The old `e47a911`/`0c8bdd8` evidence and failed-attempt logs remain preserved.
This following documentation-only record does not claim another full rerun.

| Follow-up phase | Exit | output.log SHA-256 |
| --- | ---: | --- |
| `19-framing-python-doc-format` | 0 | `fad613e2d764668c238a7b25abf468ba82f9f558d057e3c2788aff2fafd61624` |
| `20-framing-process-smoke` | 0 | `4588fe44718ee4771f37879660f5e35cde3ff124425fec8bf0eed3174cf939f8` |
