# Bounded cache expiry index

The subsequent [conservative all-expired cleanup experiment](CACHE_BULK_EXPIRY.md)
adds an upper-bound hint and a whole-cohort fast path. The implementation notes
and measurements below describe the original indexed baseline; the follow-up
document tracks its separate correctness/performance verification status.

Status: **NOT PRODUCTION READY**. This is a cache data-structure change, not an
admission-policy change or a throughput claim. The scan baseline is local commit
`683a9e5a56dd4c890234d3e02aa1125c66bb49a4`, whose published equivalent is
`ac4ffde7f64724881f683b3d1946f366faaa70be` (tree
`7d4e2f3a41ea20a28707a9e4cb7fe3e547d68339`). No dependencies, default limits,
validation rules, network behavior or persistent storage are added by this change.

## Data structure and cost

The baseline `store.add` scanned every map entry on every submission, including
live duplicates and new hashes rejected at capacity. The current implementation
keeps a deadline min-heap alongside the hash map. Each cached entry has exactly
one map association and one heap slot, and records its heap index. `Swap` updates
both indices. Deadline comparison uses `time.Time.Before`; insertion order is
irrelevant, including non-increasing supplied times and equal deadlines.

- With no expired entries, pruning checks only the heap root: O(1).
- Removing one expired entry, explicit rollback, or exact-target expiry is
  O(log N), including physical index removal. Reclaiming a cohort of K expired
  entries is O(K log N), rather than a scan. Expiry-heavy workloads can therefore
  cost more than a scan; this trade-off requires measurement.
- Accepted insertion adds one heap slot in O(log N), with occasional O(N)
  backing-array growth. Growth is geometric and clamped to `maxCount`; a rejected
  hash or live duplicate never grows the index. No up-front `maxCount` allocation
  is needed.
- Hash lookup, map updates, bytes accounting and lock acquisition still apply.
  All map/index/accounting changes use the existing cache lock. Submission lock
  order and peer iteration are unchanged.

There are no lazy tombstones. Rollbacks remove their slots immediately, even if
the same hash repeatedly fails queue admission. The heap length equals the map
length at each operation boundary, and its backing-array capacity never exceeds
`maxCount`. Removed slots are nilled, and a removed entry's index becomes -1, so
spare capacity cannot keep its transaction alive. Backing capacity may remain at
its bounded high-water mark. Each live entry now has a heap pointer slot, index
and stored hash in addition to its previous fields; this is additional metadata,
not part of the serialized `CacheBytes` budget or a process RSS limit.

## Unchanged contract

Defaults remain **1,024 entries, 16 MiB serialized transaction bytes, 120 seconds**.
The first insertion establishes the deadline. Duplicate retries, queue admission,
and successful transport writes do not refresh it or release live cache capacity.
An entry expires at equality (`now == deadline`). `get` removes only its expired
target; `stats`, idle pruning and admission pruning reclaim all entries due at
their observation time. A hash inserted again after expiry has a new deadline.

Count-full or bytes-full new hashes still return `ErrCacheFull` (HTTP 429), without
an entry or index slot. Existing duplicates are recognized before capacity
rejection. A fresh insertion that cannot reach any peer queue is still rolled
back. An already-cached hash whose retry encounters full uncovered peer queues
retains its earlier admission and deadline. There is no early eviction of live
accepted entries and no weakening of stateless validation.

The ideal long-run count-retention upper bound remains `1024 / 120 = 8.533...`
new cache lifetimes per second, before other bottlenecks. The earlier
[150-second default-TTL result](UNIQUE_CAPACITY_RESULTS_2026-11-10.md) belongs to
the **scan baseline**: 2,997 offered hashes, 1,620 accepted and 1,377 cache 429s.
It is not a measurement of this index, and its roughly 10.8 accepted/s finite
window is not steady-state throughput. It is not rerun or reinterpreted here.

## Regression design

`store_index_test.go` includes an independent map-scan reference with independent
entries and bytes accounting. Six capacity/TTL cases, two fixed random seeds and
2,000 operations per case/seed produce 24,000 differential transitions. Operations
include add, get, remove, prune, expiry and stats, with dense live cohorts,
non-monotonic timestamps, repeated/equal deadlines, exact expiry and a 1 ns TTL.
Every step compares all live entries, original deadlines and bytes, and checks:

- map/index correspondence in both directions, valid indices and heap order;
- exact serialized bytes and hard count limits;
- heap length equal to map length, backing capacity bounded by `maxCount`;
- nil spare backing-array slots, preventing retained removed entries.

Focused cases exercise expiry at equality, target-only get, same-deadline cohorts,
remove/reinsert, zero capacity, non-power-of-two capacities, byte-exact admission,
repeated rejected hashes and 6,000 remove/reinsert cycles. A concurrent test mixes
all store operations and checks invariants under the cache lock. Existing load
and short unique-capacity checks now also assert the index invariants. The eight
short unique-capacity scenarios, HTTP fail-closed fault tests, queue TTL,
peer-replacement and large-packet regressions remain intact.

Five existing test-only expiry mutations now call `setCacheExpiryForTest`, which
updates deadlines and fixes/rebuilds the heap under the same cache lock. No
production deadline-rewrite API exists. The original negative purposes and
assertions are preserved:

1. `TestLoadConcurrentSubmitHealthAndExpiry`: a second expiry epoch must free
   both cache and sent-map capacity without stale-map starvation.
2. `TestLoadTargetExpiresBeforeSentSweep`: a target must requeue with a fresh
   cache deadline before a full sent-map sweep, without deleting unrelated
   expired peer metadata prematurely or extending the retry deadline.
3. `TestLoadFullSentMapForcesReclamation`: capacity pressure must reclaim expired
   peer metadata while retaining active deduplication.
4. `TestLoadIdlePruneReclaimsExpiredMetadata`: idle cleanup must remove expired
   cache/peer metadata without submission, queueing or loss of active dedup.
5. `TestExpiredCacheDoesNotSuppressRequeue`: expired cache and peer records must
   allow a fresh queue admission of the same hash.

## Benchmarks and interpretation

`BenchmarkStoreCache` uses default cache limits and the old store API, so its
exact file can also be run on the scan baseline without the new index tests.
The [dated paired measurement report](CACHE_EXPIRY_BENCHMARK_RESULTS_2026-11-10.md)
records three samples per case on both sources, including the slower isolated
expiry reclamation and added allocations. Pair old/new runs on the same idle
host with the same Go/tool settings and preserve every sample and exit code;
do not select favorable samples or enlarge cache limits to magnify a result.
Existing Submit benchmarks provide complementary validation and per-peer work,
but are not network-throughput benchmarks.

The six store cases deliberately distinguish what is timed:

- `near_900_duplicate_prepared` and `full_1024_duplicate_prepared`: repeated
  signed objects with cached hash/size, no expiry, signing or validation timed.
- `full_1024_rejected_absent_prepared`: the **same** prepared absent hash is
  rejected repeatedly; it is not a fresh unique-hash stream.
- `near_900_add_remove_prepared`: one reused prepared hash is admitted then
  explicitly removed each iteration, with both operations timed. This models
  cache rollback cost, not sustained fresh admission or actual peer queue work.
- `expiry_cohort_1024_refill_and_prune_prepared`: one operation includes refilling
  1,024 reused prepared hashes and expiring the entire equal-deadline cohort.
  Refill is timed, not hidden, and `entries/op=1024` states the denominator. This
  uses synthetic time and is not the 120-second wall-clock capacity experiment.
- `full_1024_rejected_unique_signed_in_loop`: each timed iteration constructs and
  signs a zero-value transaction with a previously unused nonce, then computes
  hash/size and attempts admission into a full cache. The fixed public test key
  is `0x02` repeated 32 times. **Signing is included**; this is not cache-only cost,
  and all new hashes are rejected. No packet or real transaction is broadcast.

The separate `BenchmarkStorePruneExpired` times **only** one `prune` of 1,024
expired entries. Signing, hash/size warming, fresh-store allocation, refill and
postcondition checks are outside the timer and allocation measurement. Deadlines
are distinct, spaced 1 ms apart and inserted in a fixed permutation; all are due
at the observation time, including equality for the newest deadline. The same
benchmark file runs on the scan and indexed stores. Use a fixed **100 iterations**
per sample so calibration of a short prune does not amplify excluded refill work.
Its ns/op and B/op describe one entire cohort, not one transaction. Report this
separately: the old scan's O(N²) refill cost in the combined case must not mask an
indexed O(N log N) cohort-reclamation regression. Likewise, retain the admitted
entry allocations/B/op visible in the rollback and refill measurements, even if
duplicate/rejection latency improves.

Suggested paired command, only after a coordinated idle window:

```sh
GOMAXPROCS=2 go test ./internal/relay -run='^$' \
  -bench='^BenchmarkStoreCache$' -benchmem -benchtime=1s -count=3
GOMAXPROCS=2 go test ./internal/relay -run='^$' \
  -bench='^BenchmarkStorePruneExpired$' -benchmem -benchtime=100x -count=3
```

## Verification status

The first focused race attempt passed on 2026-11-10, Go 1.26.10 linux/amd64,
`GOMAXPROCS=2`, `GOFLAGS='-buildvcs=false -p=1'`, with an outer 150-second timeout:

```sh
timeout 150s go test -race -count=1 -timeout=120s ./internal/relay \
  -run '^(TestStore|TestLoad|TestCached|TestExpired|TestConcurrentDuplicateSubmission|TestSubmitAdmissionAndQueueBounds)'
```

Exit code: **0**. Go reported **1.429s** for the package, with about eight seconds
total wall time including compilation/startup. This was one run, without a failed
attempt, parameter adjustment or retry. The retained first-attempt log is named
`01-focused-race-20261110T184412Z-2.log`; it records the command, tool version,
environment, six source-file SHA-256 values, output and exit code.

A subsequent first full relay-package race attempt passed against local commit
`969cf4f52563ce9f328330d7d89bf871bdb9bfc2`, using the same Go/environment settings:

```sh
timeout 150s go test -race -count=1 -timeout=120s -v ./internal/relay
```

Exit code: **0**, package **3.961s**, about six seconds total wall time. The eight
short `TestUniqueCapacity` scenarios all passed (**1.75s** combined), along with
the other untagged relay regressions and fuzz seeds. The environment explicitly
unset `RELAY_SOAK`, `RELAY_SOAK_DURATION`, `RELAY_SOAK_SEED`, `RELAY_SOAK_REPORT`,
`RELAY_UNIQUE_DEFAULTS`, `RELAY_UNIQUE_FAILURE_PROBE` and `RELAY_UNIQUE_REPORT`.
The opt-in soak, intentional failure probe and 150-second default-TTL experiment
were skipped. The complete output and exit code are retained in
`02-relay-race-20261110T184653Z-2.log`; this was one attempt without a retry.

After adding the separate prune-only definition, a single coordinated paired
benchmark batch completed against candidate
`e43e919beed9c78204901dd93684cee5d57843ab` and the scan baseline. All twelve
commands exited 0; the new definition compiled and ran on both sources. See the
[complete results and caveats](CACHE_EXPIRY_BENCHMARK_RESULTS_2026-11-10.md).
Runtime code did not change between the full relay race and these measurements.

At this original indexed-candidate stage, full-repository race,
vet/build/smoke/fuzz/security checks and tagged official-txpool regression had not
been rerun. The later [bulk-expiry integration record](CACHE_BULK_EXPIRY.md#independent-review-and-integrated-local-validation)
records actual default and official-txpool local verification of the final
combined implementation. Initial baseline results are not reused as new evidence.
No release, new SBOM source identity or production-readiness claim is made here. Preserve
every subsequent attempt, including failures/corrections, rather than overwriting
logs or automatically repeating a failed run.
