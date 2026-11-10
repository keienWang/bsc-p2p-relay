# Unique-hash capacity evidence, 2026-11-10

Status: **NOT PRODUCTION READY**. This evidence establishes local admission and
retention behavior under synthetic loopback HTTP / in-process MsgPipe traffic.
It does not establish production throughput, long-duration stability, remote
txpool acceptance, public-network propagation, or delivery/inclusion/finality.
See [the contracts, formulas and reproduction procedure](UNIQUE_CAPACITY.md).

## Provenance and scope

- Go 1.26.10, Linux amd64, `GOMAXPROCS=2`, `go test -p=1`, `GOTOOLCHAIN=local`,
  `GOPROXY=off`, pinned BSC v1.8.2 dependencies; no parallel expensive suite.
- Harness revision `unique-capacity-v1`, baseline local commit
  `4838f5f7e9c97fb4cbb678dc81ce3e10221be81a`, baseline tree
  `acd7777b3a17b878fe87f844fe84ad2b7f4cd9fe`. The corresponding published runtime
  baseline is `d147633f1af87b02fe77d4ef9757b7b3364292fa`. These tests/docs were
  uncommitted when evidence was collected and are now saved in commit
  `6662747418c9a6308a9c5f3d48210ace9c5447bb`.
  Each artifact's environment file fingerprints the exact test and runner source.
  A subsequent comments/docs-only correction clarifies ticker pacing: no
  multi-tick catch-up queue accumulates, but at most one tick can remain pending
  and cause the next request to follow a slow request immediately. It changes no
  executable test logic or parameters and does not add another 150-second run.
- No production code/defaults changed. No P2P server/discovery, external RPC,
  public peers, real transactions, wallets or funded keys were used.
- Tests use zero-value signed fixtures with a public predictable test key and
  increasing nonces. Newly offered hashes never repeat within their scenario.
- The short proof and isolated failure probe ran before the **single** authorized
  150-second default-TTL experiment. There was no default experiment rerun,
  parameter tuning, live-cache recycling or retry-to-green.
- The execution control connection was briefly unavailable near the end of the
  default experiment. After it recovered, the original artifact directory had
  the completed JSON, full log and exit code 0. The original test completed in
  150.254s. This interruption is another reason not to interpret timing as a
  dedicated-machine performance benchmark.

The isolated capacity branch ran focused checks. A subsequent integration onto
the already-published modern-wire baseline ran the default whole-repository
checks described below; no second default-TTL experiment was performed.

## Focused race proof

Command: `RELAY_UNIQUE_RACE=1 ./scripts/run_unique_capacity.sh --short`.

Result: **PASS**, exit 0, reported package duration 2.814s. All HTTP-contract,
transport-fault, count-partition and transport-conservation regressions passed.
The eight behavior scenarios produced:

| Scenario | HTTP attempts | Distinct hashes | Old-hash retries | Valid 202 | 429 | 503 | Writes completed / failed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No peers | 1 | 1 | 0 | 0 | 0 | 1 | 0 / 0 |
| Count full | 9 | 8 | 1 | 5 | 4 | 0 | 4 / 0 |
| Bytes full before count | 3 | 3 | 0 | 1 | 2 | 0 | 1 / 0 |
| Queue rollback, partial coverage, retry | 72 | 67 | 5 | 70 | 2 | 0 | 70 / 0 |
| Actual short TTL and old-hash retry | 10 | 8 | 2 | 9 | 1 | 0 | 8 / 0 |
| Concurrent unique hashes, two peers | 64 | 64 | 0 | 16 | 48 | 0 | 32 / 0 |
| Writer failure and replacement retry | 3 | 2 | 1 | 2 | 0 | 1 | 1 / 1 |
| Continuous unique stream, short TTL | 39 | 39 | 0 | 16 | 23 | 0 | 16 / 0 |

All new-offer 202 responses had `duplicate=false`; deliberate old-hash retries
had the separately specified result. For the queue/partial scenario, **67 hashes
were ever accepted and 2 were ever rejected, with overlap 2**. There were 67
distinct hashes, not 69. Its first-offer partition was 66 accepted and 1 rejected;
retry outcomes account for the other request results. This explicitly exercises
both rejection-then-acceptance and acceptance-then-rejection for the same hash.

The count test retained all four cache entries after four successful writes;
retrying the first hash neither extended its original deadline nor queued it
again to the already-covered peer. The bytes test retained one approximately
70 KiB transaction in a valid 128 KiB byte budget while below its 16-entry limit.
The concurrent test admitted exactly 16 of 64 distinct hashes and queued each to
two peers; 48 received the exact cache-full rejection, with no resource overshoot.

The continuous short-TTL stream offered 39 unique hashes over 1.000616686s at
a configured pace of 40/s, with capacity four and **250ms** retention. Sixteen
were accepted and 23 were rejected for cache fullness. First full cache was
observed at 0.101331181s, first cache 429 at 0.127165838s, first cache deadline at
0.276987971s and admission recovery at 0.277798944s. These are accelerated logic
observations, **not default-120s or production measurements**.

## Deliberate failure-report preservation

Command: `./scripts/run_unique_capacity.sh --failure-probe`.

Result: **expected FAIL**, exit 1, 0.031s. A synthetic HTTP 500 caused the strict
gate to fail immediately. Both global and scenario JSON contain `passed=false`,
one distinct hash/attempt/response, one invalid HTTP 500, zero 202s and zero queue
admissions, plus the failed per-hash ledger and diagnostic. The complete log and
`exit-code.txt=1` were independently checked after process exit. This is a negative
gate test, not a hidden load failure and not counted as a passing capacity run.

## Single default-retention experiment

Command: `./scripts/run_unique_capacity.sh --default-ttl`.

Result: **PASS**, exit 0. Traffic interval **150.000479246 seconds**; overall Go
test package duration 150.254s. Unchanged defaults: 1,024 entries, 16 MiB serialized
bytes, 120s retention. One immediate local MsgPipe peer, queue 64, one paced HTTP
client, configured 20 new offers/s, hard maximum 3,000 offers. No prefill, retries,
peer replacement, cache mutation or forced expiration.

### Exact request and transport denominators

- **2,997 HTTP attempts = 2,997 distinct signed hashes = 2,997 valid responses**.
- **1,620 accepted 202 + 1,377 rejected 429 = 2,997**. Every rejection was exactly
  `transaction memory cache is full`; no concurrency, queue-full or expiry-admission
  error was relabeled as a successful outcome.
- **0 old-hash retries, 0 duplicate=true responses, 0 client errors, 0 invalid
  responses, 0 unexpected HTTP statuses, 0 HTTP 503s**.
- **1,620 queue admissions = 1,620 write attempts = 1,620 write completions**;
  1,620 independent local fixture decodes, 0 write failures, 0 abandoned queue
  entries and 0 inferred writer expiry skips.
- The first-offer partition and ever-outcome sets agree because this experiment
  had no retries: 1,620 accepted hashes, 1,377 rejected hashes, overlap 0.

The configured pace is not an assumed denominator. Missed scheduling ticks and
the interval boundary are not counted as offered requests, explaining why the
actual total is below the 3,000 hard maximum.

### Fill, rejection and actual TTL recovery

Offsets are seconds from the measured stream start. “Full observed” and response
events are sampled after HTTP response completion; the deadline comes from the
actual cache entry, not a simulated clock.

| Event | Seconds |
| --- | ---: |
| First full 1,024-entry cache observed | 51.252889400 |
| First cache-full HTTP 429 | 51.301541453 |
| Last 429 before first TTL recovery | 120.051155514 |
| First accepted entry's actual cache deadline | 120.052058420 |
| First 202 after the cache-full interval | 120.101202606 |

The full JSON contains all 150 per-second phase ledgers and every hash's timestamps
and first/final local state. Representative one-second intervals:

| Interval from stream start | New offers | 202 | Exact cache-full 429 |
| --- | ---: | ---: | ---: |
| [50s, 51s) | 20 | 20 | 0 |
| [51s, 52s) | 20 | 6 | 14 |
| [52s, 53s) | 20 | 0 | 20 |
| [119s, 120s) | 20 | 0 | 20 |
| [120s, 121s) | 20 | 18 | 2 |
| [121s, 122s) | 20 | 20 | 0 |
| [149s, 150s) | 20 | 20 | 0 |

Actual offered rate was 19.979936/s. The finite-window accepted average was
**10.799965/s**, because the initially empty cache contributed startup capacity
and the first cohort expired within the window. **This is not steady-state
capacity and does not override the count/retention long-run upper bound of
1,024/120 = 8.533... new cache lifetimes/s.** One first-expiry recovery is not a
multi-window steady-state or long-duration production test.

### Resource observations, not RSS guarantees

- Peak sampled cache count 1,024; serialized bytes 102,291; peer sent entries 1,024.
  This reached the count limit, far below the 16 MiB serialized-byte ceiling.
- Peak sampled queue length 0 with the immediate consumer. This only describes
  sampling instants, not an absence of transient queueing. Blocked/64+1 queue
  behavior is proven separately by the short scenario.
- Whole-harness peak sampled RSS 47,456,256 bytes, heap allocation 24,734,320 bytes,
  goroutines 17. There are 2,998 snapshots including cleanup's pre-close sample.
  Clients, decoded fixtures, bounded telemetry and the 2,997-hash test ledger are
  included. These numbers are not an isolated relay RSS budget or leak-free claim.
- The final physical snapshot had 1,023 cache entries. At the subsequent ledger
  deadline check, 1,022 accepted hashes still had unexpired cache entries, 598
  accepted hashes no longer did, and 1,377 had never been admitted. Sampling at
  different instants and expired entries awaiting pruning explain that one-entry
  difference; the ledger does not mislabel expiry as remote delivery.

## Retained artifacts

Paths below are local retained artifacts in the execution environment, not public
download links. Each directory includes `environment.txt`, `report.json`,
`test.log`, and `exit-code.txt`. Source fingerprints are in `environment.txt`.

| Run | Artifact directory | JSON SHA-256 |
| --- | --- | --- |
| Final focused race | `/tmp/bsc-relay-unique.ql4WWn` | `6ac44e499e34325bc0741d96254de7f3ad484ff2c125d23d8fe084d1099fb86f` |
| Expected failure probe | `/tmp/bsc-relay-unique.hhRQ1F` | `f96a24a0d960aadb68f48d4a01ad69aa6a89d752093447fea595ab1fc1c4106d` |
| Single default-TTL run | `/tmp/bsc-relay-unique.saNhEt` | `c932769df420fda7c173d94a23a09d88853a1466dd10bb37afca7eac4b0a9260` |

The results support the documented local contract and retention budget. They do
not remove the production gates for real traffic sizes/fanout, dedicated hardware,
multiple retention windows, long network soaks, actual txpool behavior, durable
delivery policy, or current-head verification.

## Independent review and integration verification

Independent review reproduced the short race suite (exit 0, 2.809s) and the
intentional HTTP-500 failure (exit 1, 0.025s), including its failed global/scenario
JSON and retained one-hash ledger. It recomputed the original default-run hash,
all 2,997 per-hash entries, phase totals, transport totals, occupancy and rate
denominators without repeating the 150-second experiment. The final isolated
source commit was `3d3ebb1777a2835ccbf42c42632e75c8b56fa035`; its changes after
`6662747418c9a6308a9c5f3d48210ace9c5447bb` were comments/documentation only.

The eight reviewed paths were then applied to local
`6bdefbb03eb5cb1528e7fb0ac8a2a8fa3e2595db`, whose complete tree equals public
`921b9a865e0cf4317e11f0061d826ba428cccb20`. Existing modern-wire, load, official
txpool, SBOM and security files were preserved; runtime code, modules, CI and
default configuration were unchanged.

A single serial `make check smoke vuln` with Go 1.26.10, `GOMAXPROCS=2` and
`GOFLAGS='-buildvcs=false -p=1'` completed with exit 0. Race package durations
were checkpointproof 39.000s, compatcheck 1.138s, evidence 1.067s, firstepoch
42.846s and relay 3.829s. Vet, build, formatting, all 10 Python tests, module
verification, dependency guard, smoke checks and the default vulnerability scan
passed. A separate pinned `govulncheck@v1.9.0 -test ./internal/relay` scan also
exited 0 with `No vulnerabilities found`.

The complete aggregate log SHA-256 is
`64b10b8c86f33eaef513dc2a743fa2567031840b32b34ec3fe68822d01ed22d1`;
the relay test-graph scan log SHA-256 is
`3016e51e4eac0d421674d2128bbbdefb2924b4646e0c14a1ab034977ad73fae5`.
The opt-in default-TTL experiment, intentional failure probe, known upstream
`blscacherace` diagnostic and tagged official-txpool job were not rerun by this
default aggregate command. No new fuzz or public-network run was performed.
Exact published-commit CI must still be checked separately; these local checks
do not close the documented BLS, network or production gates.
