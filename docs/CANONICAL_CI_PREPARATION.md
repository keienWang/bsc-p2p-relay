# Canonical CI build preparation

This change separates race-worker build preparation from the existing canonical
suite's time budget. One shared-cache functional validation passed. Cold hosted
preparation with the new cache namespace has not yet been validated. The
[canonical isolation experiment](CANONICAL_PROCESS_ISOLATION_RESEARCH.md) remains
test-only, with all of its production-readiness limits unchanged.

## Evidence behind the change

The canonical suite passed in both retained hosted runs:

- [Run 42967328641](https://github.com/keienWang/bsc-p2p-relay/actions/runs/42967328641/job/130347466653),
  commit `b2f12a959b65717461062366ca9ed72f78750ca8`, Go 1.26.10. This job later
  failed at its scanner step.
- [Run 42971228820](https://github.com/keienWang/bsc-p2p-relay/actions/runs/42971228820/job/130359926245),
  commit `27a1cfddce40560ac6fcbe02459de470a4c0db47`, Go 1.26.11. This job passed.

| Measured interval | Older run | Newer run |
| --- | ---: | ---: |
| Supervisor package elapsed | 53.097s | 155.697s |
| First OfficialCases RUN to first valid subtest RUN | 4.971s | 104.989s |
| Full replay test | 7.16s | 8.05s |
| Eight-request concurrent test | 9.35s | 9.26s |

The first preparation interval accounts for 100.018s, or 97.48%, of the 102.600s
package difference. It includes fixture loading/authentication, encoding, nested
worker construction, candidate generation and parent processing. It is not a
measurement of pure compilation. The older run restored about 711 MiB of cache;
the newer run missed. In the newer run, the default job saved the shared key
first and canonical subsequently failed to reserve it. These observations support
cold build preparation as the main explanation, not a demonstrated Parlia
execution regression. Runner, cache and toolchain differences prevent a controlled
performance comparison.

SHA-256 of the retained decoded UTF-8 canonical job logs, in the same order:

- `68674ac603a0300a31f66c01c7069972bf1bc96657cca5ae940b0c7e14043e3a`
- `b37ba2eb61fd113483c9d4f5ecd5e93d72720e441481299fbb9fa7bdec547205`

The newer default-job log confirming the first save has SHA-256
`bc6f53e0b0edb31ffc3bffa6a17a08d58bf7981b6768136a1135848fa1237739`.

## Preparation and cache behavior

Immediately before the original suite, the checked-in script uses the same
`internal/canonicalisolation` working directory and inherited environment as the
nested test helper:

```sh
go build -race -tags=canonicalisolation -o <private-temporary-directory>/worker ./worker
```

The build receives 175s before TERM and 5s before KILL; the preparation step has
an additional 4-minute outer limit. Its private output is removed on normal
success or failure return and is never executed or passed to the suite. Failure
is not ignored or retried. The suite still builds its own worker from source.
The outer suite's ldflags are deliberately absent from both worker builds.

The original helper 120s, suite 180s, job 15min, child 60s, two-slot limit,
Wait/reap handling, response binding, rejection cases, replay and binary-boundary
assertions are unchanged. Preparation adds a cached build/link pass and may add
total work; it makes no speedup promise.

The existing pinned setup-go action
`924ae3a1cded613372ab5595356fb5720e22ba16` defaults to hashing root go.mod and
omits job/tags/race from its key. It does not update a primary-key hit. Canonical
now supplies root and local-replacement mod/sum files, canonical Go sources and
the preparation script as explicit hash inputs, separating it from sibling jobs.
The exact pinned action's bundled glob/hash implementation was checked offline:
11 files matched, producing suffix
`49909c830734397f6f4d5e9a739517ca725408499cb1b441b51cd01bb71dcd9b`.
Cache availability remains an optimization; misses must still complete the full
bounded build and suite. A future warm hit is not guaranteed.

## Single local functional validation

The implementation tested locally was commit
`4d1e52040c2bf5de017a4f64db31e037428191cb`, tree
`f33040807833253839e3c4be2c77a60bd546ee9e`. This document was added afterward,
without changing the tested implementation or rerunning heavy tests.

One serial, locked window used Go 1.26.11, CGO_ENABLED=1, GOMAXPROCS=2,
`-p=1`, readonly module flags, disabled workspace/toolchain switching, offline
module access and an existing shared build cache. Four independently generated
fixture files were reused only after their actual hashes matched all four
compiled pins; this was not fresh fixture reproduction. The original command ran
once:

```sh
go test -race -ldflags='-s=false -w=false' -count=1 -timeout=180s -v ./internal/canonicalisolation/...
```

- All 61 original tests/subtests and all three packages passed without skips.
  Package elapsed: supervisor 88.925s, protocol 1.015s, worker 1.491s.
- Full replay passed in 11.49s; the eight-request concurrent case passed in
  15.95s with eight distinct PIDs, peak two and zero active children after Wait.
- Preparation took 9s and its output was absent before the suite. The suite's
  own distinct worker build took 4.415s; one-time fixture preparation took
  7.002s, including that build. Process timing explicitly excludes slot waiting.
- Original failure/input checks and dependency/buildinfo/nm assertions passed.
  Temporary outputs were empty afterward; source identity was unchanged.
- All stages exited zero in approximately 110s within a 450s allocation. The
  final manifest was generated after logs and exit records closed; all 31 file
  hashes were independently rechecked. Its SHA-256 is
  `1207e232ab7aae879b189aff0ea779f494eeb3da006a0e79daae1ccbfef915da`.

Before that window, all 46 Python tests, shell syntax, workflow structure and
patch-whitespace checks passed. The new helper contract deliberately checks one
known function shape. The fake-go exit-124 case tests status propagation and
ordinary EXIT cleanup, not actual 175-second termination, KILL escalation or
general descendant reaping.

## Remaining limits

This evidence covers one shared-cache functional run. It does not prove cold
startup, cross-runner performance, long-term stability, arbitrary process-tree
cleanup or production readiness. A real hosted cache miss under the new
canonical namespace is still needed for cold-preparation evidence.

Scanner test-graph coverage remains a separate unresolved issue and is not fixed
by this preparation change. Scanner commands and dependencies were not changed,
no scanner was rerun in this local window, and existing zero-findings results
must not be treated as proof of complete test-graph coverage. No external BSC,
RPC, transaction or production service was exercised.
