# Unique-transaction capacity and retention

Status: **NOT PRODUCTION READY**. These tests and budgets describe bounded local
admission, not public-network propagation, remote txpool acceptance, inclusion,
execution, finality, or a production throughput/SLO claim. This change modifies
tests, documentation and an opt-in runner only. It does not change validation,
runtime admission policy, default limits, retention, or networking.

## Why repeated HTTP requests are not fresh transaction throughput

The earlier [occupancy harness](SOAK.md) mainly resubmits its 900 or 1,024 prefilled
hashes. Its nominal 800 HTTP requests/s measures repeated admission/deduplication
and reconnect work, **not 800 new signed hashes/s**. The fresh-submit microbenchmark
also explicitly recycles fixture entries. Neither demonstrates sustainable intake
of new hashes under the real retention policy.

The new harness signs a transaction when each new offer is made, using the public
test-only key `0x02` repeated 32 times, monotonically increasing nonces, zero value,
and no funded account. New-offer hashes must not have appeared earlier in that
scenario. No arbitrary cache deletion or simulated clock advance occurs. Explicit
old-hash retries have their own labels, counters and ledger entries, even when
cache expiry or a previous rejection makes the API return `duplicate=false`.

All HTTP goes to a literal `127.0.0.1` listener, without a proxy or redirects.
The production handler, stateless signature validation, cache, admission and writer
run normally. Peers use in-process BSC MsgPipe, the pinned official dual handshake
and packet decoder. No P2P server is started. No discovery, external RPC, public
peer, wallet, real payment or private project is involved. These peers do not run
a stateful txpool; packet decode does not establish txpool acceptance.

## Verified runtime admission contract

Source: `internal/relay/node.go`, `store.go`, and `http.go` at baseline tree
`acd7777b3a17b878fe87f844fe84ad2b7f4cd9fe` (local commit
`4838f5f7e9c97fb4cbb678dc81ce3e10221be81a`).

1. The HTTP handler has eight active transaction-request slots. Exhaustion returns
   429 `too many concurrent requests` before parsing or touching the cache.
2. Raw transactions pass stateless validation before submission takes `submitMu`.
   The lock serializes cache admission and per-peer deduplication. Concurrent
   distinct hashes cannot independently oversubscribe count/byte limits.
3. With no `receiveTx` peer in the current map, submission returns 503
   `ErrNoPeers` **before inserting into the cache**. A session whose `done` is
   already closed but is momentarily still in the map can pass this initial
   availability check, then be skipped and produce a queue 429. Ready-peer counts
   are readiness hints, not a future-delivery guarantee.
4. Cache admission first prunes expired entries, then recognizes a duplicate
   before checking capacity. A new hash is rejected with 429 `ErrCacheFull` if
   count is full or its serialized size will not fit the remaining byte budget.
   Count and bytes share this HTTP error string; the test knows which limit was
   reached from its configuration and exact occupancy, not from an invented API
   reason. No new cache entry remains after this rejection.
5. Fresh entries expire at **first insertion + TTL**. Cache duplicate retries,
   successful queue admission and successful writes do not extend that deadline.
   Writes do not remove cache entries or per-peer `sent` records. Success therefore
   does not create fresh capacity before expiry.
6. Each receiving, not-done peer is considered independently. Its `sent` map means
   “successfully enqueued during this cache lifetime,” not “peer accepted.” It is
   updated only on queue success, bounded by `CacheCount` per peer, and retains the
   cache deadline. Target hash expiry is checked exactly; ordinary map sweeps are
   throttled, while reaching the bound forces a sweep. Idle maintenance also
   prunes metadata.
7. **Any positive `queued_peers` returns 202**, including partial fanout. A slow
   or full peer may receive nothing. A retry can later enqueue the same cached
   hash to an uncovered/replacement peer. If all considered peers already have
   valid sent records, a duplicate can return 202 `stage=cached`, `queued_peers=0`.
8. If no new queue admission is possible and an eligible uncovered peer exists,
   submission returns 429 `ErrQueuesFull`. A **fresh** insertion is rolled back.
   A **previously cached** hash remains cached, preserving earlier accepted
   admissions. Thus the same hash can have both an earlier 202 and a later 429;
   the 429 does not retract earlier queueing. A previously rejected fresh hash
   can later be retried and receive 202 with `duplicate=false`.
9. If the deadline passes during admission, 429 `ErrAdmissionExpired` is possible.
   Its entry is already expired and will be pruned; it is not a new live lifetime.
   These short tests do not treat that rare timing outcome as a generic success.
10. Each production peer has a queue of 64 outbound items, plus at most one item
    being written. The writer skips a transaction whose deadline has passed
    **before starting its write**. An already-started write is not cancelled at
    TTL, and may finish after expiry. A write failure disconnects the peer and
    increments the failure counter; it does not free the cache. Peer/process
    exit may abandon queued transactions. The existing expiry fixture in
    [SOAK.md](SOAK.md) separately proves queued expiry skips and late completion.

No runtime bug or policy change is asserted by these tests. In particular, 202
is a best-effort local queue contract, not durable delivery. Turning it into a
promise that accepted transactions cannot be lost requires a separately reviewed
policy, persistence/acknowledgments and observable failure states. Do not hide
expired/abandoned entries or redefine them as delivery success.

## Capacity budget, including what CacheBytes does not bound

Let `T` be retention seconds, `lambda` the required sustained **new-hash admission**
rate, and `B` the additional unique-hash burst above that rate. A conservative
count budget under that arrival envelope is:

```text
CacheCount >= ceil(lambda * T) + B
```

More precisely, the number of unexpired accepted new hashes in every rolling
retention window must fit. Deduplication retries do not add cache count during a
lifetime, but still consume HTTP parsing, signature validation, lock, and peer
iteration resources. “New” here means new hash, not new HTTP attempt, nor a
transaction that is necessarily valid against current chain state.

For serialized bytes, budget the maximum rolling-window sum, for example:

```text
CacheBytes >= serialized_new_bytes_per_second * T + serialized_burst_bytes
```

Average transaction size is insufficient for an adversarial or heavy-tail size
distribution. Use an explicit size envelope, percentile plus overflow policy,
or the conservative `CacheCount * maximum_supported_serialized_size`. Count and
bytes must both fit. The accepted individual raw-size limit remains 128 KiB;
`New` also requires a cache byte limit of at least that size. The bytes-first test
therefore uses a valid 128 KiB cache and approximately 70 KiB transaction payloads,
not an impossible CLI configuration.

Default limits are 1,024 entries, 16 MiB serialized bytes, and 120 seconds. Even
with instantly successful writers and tiny transactions, the ideal long-run
**new cache lifetime** admission upper bound from count alone is
`1024 / 120 = 8.533... per second`. Byte retention can impose a smaller bound.
Slow peers, HTTP admission, validation and CPU can further reduce it. This is a
retention constraint, not a measured throughput guarantee.

The present `store.add` calls `pruneLocked` on **every submission**, scanning the
whole cache map, including duplicate and eventually rejected offers. That is
O(CacheCount) work under the submission/cache locks near capacity; health reads
also prune. Enlarging count/retention therefore changes admission CPU/lock cost
as well as memory. Per-peer iteration, occasional sent-map sweeps and fanout add
further work. The count formula is a storage feasibility condition, **not a
performance guarantee** or a reason to increase limits without target-machine
measurements. This batch does not redesign cache eviction/pruning.

An initially empty cache admits an initial burst; another burst can begin when
that first cohort expires. A finite-window average, including a window just past
120 seconds, can therefore exceed 8.53/s without violating any limit. Do not call
that steady state. Evaluate rolling occupancy and admissions after a full
retention warmup over **multiple** retention windows before making operational
claims. The proposed 150-second experiment below demonstrates one recovery, not
steady state.

`CacheBytes` is **not an RSS limit**. Budget and measure separately:

- Decoded transaction objects, serialized encodings/caches, map buckets, allocator
  overhead, GC headroom and transient validation/HTTP buffers.
- Per-peer sent metadata: **O(peer_count × CacheCount)** hashes, deadlines and map
  overhead. Increasing cache count also increases the peer-metadata budget.
- Up to **64 queued + 1 in-flight item per peer**, fanout encoding and transport
  buffers. Payload references may share cache objects, but retries and later cache
  lifetimes need not share allocations. Queued or in-flight references can keep
  old transactions alive **after cache eviction/expiry**; an in-flight write may
  remain blocked beyond TTL. Protocol-response items also occupy these queues.
- Slow/blocked peers and replacement sessions, handshakes and pending peers, P2P
  read buffers, protocol request/response payloads, sockets and goroutine stacks.
- Clients, signed fixtures, per-hash ledger, packet observers, latency arrays and
  sampling in this test process. Harness RSS/heap are not isolated relay RSS.

Prefer a fixed reviewed resource budget and explicit backpressure. Do not “fix”
429s by silently evicting previously accepted live transactions, enlarging the
defaults, disabling signature validation, or quietly reducing retention. High
fresh rate + long retention + small memory may be incompatible. Choices must be
explicit: bounded caller retry/backoff, an approved shorter supported retention,
reviewed larger resources/fanout changes, or a different admission/delivery policy.
Retries to a partial fanout retain the original deadline, so callers cannot assume
that retrying resets their opportunity to reach an uncovered peer.

Minimum business inputs still needed, rather than guessed here:

1. Required new hashes/s, burst size/duration, and signed transaction size mix.
2. Required retention/retry window and acceptable rejected-offer behavior.
3. Fanout requirement: one local queue, all selected peers, or independently
   observed remote txpool acceptance? What are the slow-peer assumptions?
4. Target hardware/process memory budget and latency, availability, delivery and
   recovery SLOs, including restart behavior.

## Short proof and strict accounting

Run with Go 1.26.10, pre-cached pinned dependencies, and no competing expensive
test process:

```sh
./scripts/run_unique_capacity.sh --short
RELAY_UNIQUE_RACE=1 ./scripts/run_unique_capacity.sh --short
```

The runner forces `GOMAXPROCS=2`, `GOTOOLCHAIN=local`, `GOPROXY=off` and `go test -p=1`.
It takes the same nonblocking `flock` as the older soak runner. No tests use
`t.Parallel`. Ordinary CI can run these short logic/fault tests; the default-120s
experiment is skipped unless explicitly enabled.

The eight scenarios cover no-peer 503, count-full retention after successful
writes, bytes filling before count, blocked writer plus 64-item queue, fresh 429
rollback, partial fanout, old-hash retries including a 429 that preserves earlier
admission, actual 250ms-TTL recovery, four clients submitting 64 different signed
hashes under a 16-entry limit, write failure/replacement, and a one-second paced
unique stream. All new-hash 202 responses require `duplicate=false` and the exact
expected queue count/range. Old-hash retries have explicit duplicate expectations.
The shortened TTL cases are **logic proofs, not measurements of default 120s
retention**. Timing/CPU competition during these tests is not a benchmark result.

HTTP validation reuses the existing strict object/schema gate: exact fields,
no missing/extra/duplicate/null fields, expected hash/sender, consistent stage,
bounded queue count, JSON content type, bounded complete body and successful read
and close. Each request predeclares allowed statuses and exact rejection reasons.
No unexpected 400/500/503 or wrong 429 reason is swallowed. Client failures and
invalid responses remain in denominators and fail the scenario. Separate fault
injections verify these failure paths; they never enter the measured stream.

Every scenario records a **per-hash ledger**, keyed by hash: nonce, first-offer
phase/status/validity, total attempts, retry attempts, last response, whether it
ever had a valid 202 or rejection, queue admissions, observed first/final cache
deadlines, final local cache state, and unexpired local per-peer sent coverage.
Per-peer fixture decode counts remain separate from writer outcomes and from
that local sent coverage. No field claims remote acceptance or chain state.

The following partitions are checked for all attempts and for each labeled phase:

```text
HTTP attempts = offered distinct hashes + old-hash retry attempts
HTTP attempts = received responses + client errors
responses = valid 202 + valid 429 + valid 503 + invalid responses
responses = sum of actual HTTP status counts
valid 429 + valid 503 = sum of exact rejection-reason counts
all counters = sum of corresponding phase counters
distinct hashes = first-offer 202 + first-offer rejection + invalid/client first offers
per-hash attempt/retry/queue totals = global attempt/retry/queue totals
queue admissions = write attempts + abandoned queued items + inferred expiry skips
write attempts = write completions + write failures
```

The sets “ever accepted” and “ever rejected” **overlap**. A hash rejected first
and accepted on retry belongs to both; they must not be added to count unique
offers. The report explicitly gives their overlap. First-offer outcomes partition
distinct hashes; per-attempt outcomes partition requests. This remains true after
TTL causes an old-hash retry to return `duplicate=false`.

Expiry skips are inferred only after teardown completes all writer outcomes and
only when queue/write/abandonment accounting and observers are valid. A broken
ledger fails instead of inventing a skip count. Cache count, exact serialized-byte
sum, per-peer sent bound and queue bounds are checked throughout. Samples contain
cache/queue/sent occupancy, process heap/RSS and goroutines. Successful writes,
failed writes and fixture decodes have independent counters. Occupancy snapshots
count physical entries at their sampling instant; the final per-hash ledger
checks deadlines at its own later instant. An expired entry awaiting pruning can
therefore appear physically present but not unexpired in that ledger.

Each run creates a new `/tmp/bsc-relay-unique.*` directory containing
`environment.txt` (Go, OS, revision, source fingerprints, process settings),
`report.json` (phases, ledgers, occupancy, transport outcomes, measured intervals),
`test.log` (complete stdout/stderr including failures) and `exit-code.txt`.
Reports use exclusive creation. Early failures still finalize scenario evidence;
a process kill/global Go timeout can prevent JSON cleanup, so missing JSON is
never success. Keep the full log and nonzero exit code. The only finite-stream
rate denominator is its actual measured interval, excluding fixture setup and
teardown; the report also records total scenario elapsed time separately.

An isolated negative probe verifies report preservation after a Fatal HTTP gate:

```sh
./scripts/run_unique_capacity.sh --failure-probe
```

This command **must exit 1**, with `passed=false`, one offered hash/attempt,
one invalid HTTP 500, no admitted queue items and a retained per-hash ledger.
It runs alone, is skipped by ordinary tests, and is not a performance sample.
It does not retry or convert the deliberate failure into a passing experiment.

## Opt-in default-120s experiment

This requires an explicitly operator-approved run, not an automatic CI job. One
authorized run was completed on 2026-11-10; see the linked results below:

```sh
./scripts/run_unique_capacity.sh --default-ttl
```

- Fixed **150 seconds of offered traffic**, about 2.5 minutes plus build/setup,
  with a 180-second Go test timeout. One test process, `GOMAXPROCS=2`, `-p=1`.
- Unchanged default **1,024 / 16 MiB / 120s**, one immediate local MsgPipe reader,
  queue 64, no churn, no prefill, no retries, zero-value approximately 100-byte
  signed fixtures generated with increasing nonce on every offer.
- One closed-loop client paced at **20 unique offers/s**: fewer than or about
  3,000 offers. There is no multi-tick catch-up queue, but at most one tick can
  remain pending, so the next request may immediately follow a slow request.
  Actual offer count/rate is reported.
- Expected qualitative sequence: cache fills, exact `ErrCacheFull` 429s appear,
  then new hashes enter as the first accepted cohort really expires after 120s.
  A pass requires rejection and more than 1,024 accepted unique hashes, with
  acceptance observed beyond one TTL. No exact timing-dependent count is assumed.
- A hard maximum of 3,000 offers prevents accidental unbounded traffic.
- Per-second phase outcomes, first-full-cache/first-429/first-deadline-recovery
  timestamps, every hash's first/final local state, writer/fixture
  counts, cache bytes/count and process-resource samples are retained in the same
  four artifacts above. No production runtime observer is added.
- Startup capacity and only one full TTL make any overall accepted/s figure a
  **finite-window average, not sustainable production TPS**. This test is not a
  multi-hour leak check, TCP/RLPx network fault test, real txpool test or SLO study.

Do not start or repeat this run merely because its command is documented. Arrange
an idle, budgeted machine and preserve its outputs. The short-proof results and
single authorized default-TTL run are documented separately in
[UNIQUE_CAPACITY_RESULTS_2026-11-10.md](UNIQUE_CAPACITY_RESULTS_2026-11-10.md).
