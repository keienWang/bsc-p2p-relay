# Opt-in HTTP / MsgPipe load harness

Status: **NOT PRODUCTION READY**. This is a small, reproducible local acceptance
fixture, not a long-duration network soak or production throughput benchmark.
It changes no production code, validation, capacities, or verification rules.
The current harness revision is `http-fail-closed-v2`; its HTTP gate replaces
the incomplete original acceptance checks. See [the validation correction and
new evidence](SOAK_HTTP_VALIDATION.md).

## Reproduce

Use Go 1.26.11 and the repository's pinned BSC v1.8.2 dependencies, downloaded
before the run. The script uses `GOTOOLCHAIN=local`, `GOPROXY=off`, a shared
`flock`, and GOMAXPROCS=2 by default. It starts at most one load process and
preserves environment metadata, a JSON report, the full log, and exit code in a
new temporary directory. A report is created exclusively, never overwritten.
A process kill or Go timeout may prevent JSON cleanup output; retain the full
log and nonzero exit code instead of interpreting a missing report as success.

```sh
./scripts/run_relay_soak.sh
RELAY_SOAK_RACE=1 ./scripts/run_relay_soak.sh
```

Equivalent direct invocation, after choosing a **new** report path:

```sh
GOMAXPROCS=2 RELAY_SOAK=1 RELAY_SOAK_DURATION=5s \
  RELAY_SOAK_SEED=20261110 RELAY_SOAK_REPORT=/tmp/relay-soak-new.json \
  go test ./internal/relay -run '^TestSoak(MsgPipe|Quantiles)$' \
  -count=1 -timeout=60s -v
```

The duration is **per occupancy scenario**, not total process duration. The
ordinary `go test ./...` suite skips the timed load; only the small percentile,
strict HTTP response and deterministic fault-injection regressions run. The fault tests have separate clients/counters and never
contribute observations to the timed report. Do not add the timed soak to every
pull request's CI.

An operator may opt into a longer run on a designated machine, after checking
its resource budget. This command is an example, **not a run already performed**:

```sh
RELAY_SOAK_DURATION=30m RELAY_SOAK_TIMEOUT=65m ./scripts/run_relay_soak.sh
```

The supported duration range is 3s..1h per scenario. With the default two-minute
cache TTL, a long run includes cache expiry/repopulation periods. It does not
claim 100% occupancy at every moment; the JSON records actual occupancy. Final
full-cache probes are explicitly recorded as skipped if the cache is not full.
For RLPx smoke coverage separately run the existing
`TestLoopbackRLPxOfficialBSC`. This harness does **not** replace that test.

Run the failure-gate regressions separately from a measured load:

```sh
go test -race ./internal/relay \
  -run '^TestSoakHTTP|^TestSoakMixed|^TestSoakQuantiles' \
  -count=1 -timeout=30s -v
```

The script fingerprints all `soak*_test.go` sources, reports
`harness_revision=http-fail-closed-v2`, and still runs only the timed harness
and small quantile check. The fault-injection cases are not performance samples.

## Workload and safety boundary

**The approximately 800/s pace below is repeated HTTP requests over prefilled
hashes, not 800 newly signed transaction hashes/s.** Successful writes retain the
cache until the original TTL, so default count retention alone bounds ideal
long-run new cache lifetimes to 1,024 / 120s, about 8.53/s. Startup bursts and expiry
boundaries can raise short finite-window averages. See the separate
[unique-hash capacity proof and resource budget](UNIQUE_CAPACITY.md).

- Only an HTTP server on literal `127.0.0.1:0` opens a listener. No P2P server is
  started, no discovery occurs, and no external RPC or public peer is contacted.
- Transactions use the existing predictable, test-only key and zero value. They
  are synthetic protocol fixtures, not a funded account, real wallet, or payment.
- Separate scenarios prefill **900** and **1,024** entries through the real HTTP
  handler, signature validation, cache admission and production writer. Cache
  capacity remains 1,024 entries / 16 MiB and the TTL remains two minutes.
- These fixtures are about 100 bytes each, so the test reaches the **entry**
  limit, not the 16 MiB byte limit. Existing byte-limit tests remain necessary.
- Sixteen HTTP clients each offer at most one request every 20ms, about 800/s
  total. Requests are closed-loop, paced, with no unbounded backlog. Scheduling
  and missed ticks can reduce achieved throughput. This is not an open-loop
  overload capacity estimate and does not correct coordinated omission.
- Fast readers, a 75ms slow reader, and a reader that never consumes transaction
  messages use upstream BSC MsgPipe, official eth/68 dual handshake and packet
  decoder. Both fast and blocked sessions are replaced with the same peer IDs
  every two seconds. Cache retries exercise the new session's deduplication.
- The scenario closes transports and stops writers, counts remaining queue
  entries, and waits for fixture goroutines. Cache, peer-map and queue bounds are
  asserted. Old session cleanup closures are not accumulated during long runs.
- A separate 250ms-TTL probe admits one blocked in-flight write and 64 queued
  transactions. One additional request must be rejected with 429. After TTL,
  the in-flight write completes, while 64 queued entries must be skipped. This
  is deliberate evidence that an **already-started write is not TTL-cancelled**.
- Seed 20261110 fixes the fixture-index offset. It does not make Go scheduling,
  TCP HTTP timing, or contention deterministic. HTTP uses no proxy and a bounded
  connection pool, and every response body is consumed and closed.

## What the measurements mean

1. `http_roundtrip_*`: client request start through reading the response body.
   It includes loopback TCP, HTTP admission/rejection, response and scheduling.
   `http_handler_admission_and_response` separately measures the server handler,
   including JSON/signature validation, submit lock, enqueue and response work.
   Neither isolates lock wait or the exact instant of queue insertion.
2. `*_cache_to_write_start`: cache insertion to the observed underlying
   `MsgPipe.WriteMsg` call. The timestamp comes from cache expiry minus TTL.
   **This is cache age / an upper bound on queue residence, not exact queue
   latency.** A duplicate retried after reconnect can have seconds of old cache
   age even if it enters a queue and writes immediately. Missing origins are
   counted. No production observer hooks or enqueue timestamps were added.
3. `*_pipe_write_completed` and `*_pipe_write_failed`: underlying synchronous
   MsgPipe call duration, separated by outcome. MsgPipe completion means its
   local consumer consumed the payload (and has close-specific semantics); it
   is not TCP/RLPx flush, remote txpool acceptance, inclusion or finality.
4. `*_write_start_to_fixture_decode` and `*_cache_to_fixture_decode`: time until
   the **local official packet decoder** finishes and the expected hash matches.
   Decode counters remain independent of write completion counters.

The wrapper copies and decodes each small transaction before the timed pipe
call. HTTP clients, fixtures, memory sampling, GC and latency bookkeeping share
one process with the relay. Observer overhead affects measurements. RSS and Go
heap therefore describe the whole harness, not an isolated relay service.

All observations are counted. Up to 65,536 observations per metric, p50/p95/p99
are exact nearest-rank values in nanoseconds. Beyond that, a fixed-size histogram
reports lower/upper bounds containing each nearest-rank percentile; it never
labels sampled/approximate values exact. Bucket width is at most about 0.391%
above 256ns. Exact sample storage is capped, with no duration-proportional event
stream. Once-per-second resource snapshots are bounded by the one-hour limit.

Transport counters include prefill, while transport latency series reset after
prefill has completed. `warmup_queue_admissions` gives the prefill denominator.
The original `http_*` counters and HTTP latency exclude prefill and include
labeled final probes; `active_http_requests` and its throughput exclude those
final probes. The new `all_http_*` counters include prefill as well, so a warmup
failure cannot disappear into an unmatched error denominator. In both groups,
`*_requests` means responses received, not attempted requests; `*_attempts`
includes client failures. `phase_*_attempts` partitions the full request count.
Response schema validation occurs after the measured client response-body read;
its CPU/allocations affect the shared harness but are not added to that RTT.

## Failure accounting, not just success percentiles

Every response is checked against a phase-specific contract, including prefill
and expiry. HTTP 202 requires exactly the five submission fields, expected hash
and sender, a nonnegative bounded queue count, and consistent stage/duplicate
semantics. Fresh single-peer admissions must be queued once with duplicate=false.
JSON must be one complete object with no missing, extra, duplicate or null fields.
Responses must have JSON content type, fit the body bound and finish reading and
closing without error. Redirects are not followed.

During active load, the only accepted rejections are HTTP 429 with the exact
queue-full, concurrency-full or admission-expired reason. The explicit full-cache
and full-queue probes each require their own exact 429 reason. **HTTP 503 is
allowed only for the explicit probe after all peers stop, with ErrNoPeers.**
Reconnect windows do not permit 503 because the slow peer remains connected.
Unexpected HTTP 400/500 or mismatched status/error semantics fail the test.

Both occupancy and expiry invoke the same fail-closed audit. It checks, for both
all-phase and measured counters:

```
attempts  = received responses + client errors
responses = valid responses + invalid responses = sum of HTTP status counters
invalid responses = sum of mutually exclusive read/body/header/decode/schema/status/semantic errors
valid responses = valid 202 + valid 429 + valid 503
all attempts = sum of phase attempts
measured attempts = all attempts - prefill attempts
```

Any client error or invalid response fails acceptance, even if these identities
balance. The report retains every error count and up to eight diagnostic messages
(each capped at 512 bytes). Invalid responses cannot add queue admissions. The
expiry scenario additionally requires exactly one local fixture decode. Warmup
writer completion has a one-second deadline rather than relying on a long-run
global timeout.

The report contains every HTTP status/rejection reason, client/read/decode error,
write attempt, successful/failed write, local fixture decode, session disconnect,
reconnect and abandoned queue entry. It checks the conservation identity after
all sessions have stopped:

```
queue admissions = write attempts + abandoned queued entries + expired skips
write attempts  = write completions + write failures
```

`expired skips` is inferred from that identity using the production writer's
single non-write path, rather than modifying production counters. An observer
error invalidates that inference and fails the test. It is not a measurement of
cache-entry expiry. Abandoned queues can contain expired entries at disconnect;
they remain attributed to disconnect, not double counted as writer expiry.

Disconnected queue entries are **censored/unwritten outcomes**, not successful
latency samples. Their individual residence times are not available. Failed
in-flight write durations have separate percentiles. Slow-peer write failures,
429s and after-expiry completions are expected evidence, not hidden by retries.
A 429 may occur even after a fast peer was already queued in an earlier request,
when newly eligible slow/blocked peers currently have full queues. It does not
retract that earlier queue admission.

## Interpretation and remaining work

A short passing run demonstrates bounded configured data structures, exercised
backpressure, deterministic expiry behavior and cleanup for this workload. A few
seconds of RSS/heap/goroutine observations **cannot establish long-term stability
or a leak-free production service**. Final heap remains above the initial sample
because fixed-capacity telemetry arrays have been allocated; process RSS need
not fall immediately after GC. Report both the time series and cleanup count.

Follow-up work still needed:

- Add reviewed, low-overhead enqueue timestamps / queue-drop metrics before
  claiming exact admission-to-queue and enqueue-to-write percentile SLOs.
- Reproduce on a dedicated target machine, repeat different seeds and load rates,
  include large payloads / near-byte-capacity cases and open-loop traffic.
- Exercise actual loopback RLPx/TCP backpressure, and then authorized independent
  public testnet peers, real txpool rejection and retention. MsgPipe is a
  deliberate stronger synchronous-reader pressure fixture, not a network model.
- Run budgeted multi-hour operational soaks with network faults, memory profiles,
  process limits and alert/rollout procedures. Keep raw failures and denominator
  counts alongside successful latency summaries.
- Resolve the upstream modern BLS-cache concurrency blocker separately. Nothing
  in this broadcast harness enables modern consensus verification or changes
  the public-network reachability limitation.
