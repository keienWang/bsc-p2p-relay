# Conservative bulk expiry: three-implementation measurements

Status: **NOT PRODUCTION READY**. This report measures a narrow store operation
and two prepared no-expiry paths. It does not establish HTTP or network latency,
new-transaction throughput, remote txpool acceptance, a production SLO, completed
GC, or lower RSS. See the [invariant and correctness evidence](CACHE_BULK_EXPIRY.md).
The earlier [42-sample report](CACHE_EXPIRY_BENCHMARK_RESULTS_2026-11-10.md) is
preserved unchanged and is not reclassified as evidence for this follow-up.

## Exact sources and execution

- **scan:** `683a9e5a56dd4c890234d3e02aa1125c66bb49a4`, tree
  `7d4e2f3a41ea20a28707a9e4cb7fe3e547d68339`. Its separate worktree had no tracked
  changes and only the expected added benchmark file. It ran the original store,
  not the scan-reference unit-test model.
- **heap:** `df9e5c299da3a6a65ba81cff5137c74414cf6a8e`, tree
  `143395a8df8aeb9669b00fcd7ad451e3c941d500`, clean and frozen.
- **fast:** `6109ca14516a3c251cffcda741558c048241adf9`, tree
  `1af622303540c8178059fead35a0f4dd72452bee`, clean during measurement. Its full
  untagged relay race suite had passed once beforehand: exit 0, 3.858s, including
  the new bound/bulk-clear regressions, 24,000 differential operations and eight
  short unique-capacity scenarios.
- All three used the exact same `store_bench_test.go`, SHA-256
  `50bae83e7bbb462179f6ee608e054a870481644602861aaecdc444ae93921283`.
  No runtime or fixture edits occurred during the batch.
- Go 1.26.10, linux/amd64, `INTEL(R) XEON(R) PLATINUM 8573C`, `GOMAXPROCS=2`,
  `GOFLAGS='-buildvcs=false -p=1'`. Race instrumentation was off for timing. Every
  `RELAY_SOAK*` and `RELAY_UNIQUE*` environment variable was unset.
- One batch, **2026-11-10 19:07:44–19:08:55 UTC**, **71 seconds**. The three rounds
  used **scan→heap→fast, heap→fast→scan, fast→scan→heap**. Each implementation ran
  prune first, then the two hot paths. All **18 commands and the batch exited 0**.
  All **27 samples** are below; no failure, retry or discarded sample occurred.
- Each command had a 30-second Go timeout and 35-second outer hard limit, within
  an overall 180-second hard limit. No other project heavy job was deliberately
  started during the window. This was not a dedicated or CPU-pinned host.

Each implementation in each round ran exactly:

```sh
timeout -s KILL 35s go test -timeout=30s ./internal/relay -run='^$' \
  -bench='^BenchmarkStorePruneExpired$' -benchmem -benchtime=100x -count=1
timeout -s KILL 35s go test -timeout=30s ./internal/relay -run='^$' \
  -bench='^BenchmarkStoreCache$/(full_1024_duplicate_prepared|full_1024_rejected_absent_prepared)$' \
  -benchmem -benchtime=1s -count=1
```

`02-three-way-batch.log` retains complete commands, source states, output and exit
codes (SHA-256 `4dad393a08ba4d62d627f9662ea3a295a8f092fb893cd01c80407f3b54f2253b`).
`02-three-way-summary.json` contains the parsed samples and medians (SHA-256
`85f59f39a8ca6b5360b6e80f1fb6cf272fcc43c1b89f09529b51ba73c6a3ccaa`). The eighteen
individual command logs also retain their timestamps, output and exits. Verbatim
result lines are included below so no sample is hidden behind an external log.

## Results and limits

Medians use all three samples. Prune is one **entire 1,024-entry cohort** per
operation; the other operations are individual repeated prepared offers.

| Case | Scan ns/op | Heap ns/op | Fast ns/op | Scan B/op / allocs/op | Heap B/op / allocs/op | Fast B/op / allocs/op |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| All 1,024 staggered entries due: prune only | 63,568 | 275,872 | 8,902 | 0 / 0 | 0 / 0 | 0 / 0 |
| Full cache duplicate, prepared object | 15,699 | 42.46 | 38.35 | 0 / 0 | 0 / 0 | 0 / 0 |
| Full cache same absent prepared hash rejected | 15,126 | 42.38 | 46.82 | 0 / 0 | 0 / 0 | 0 / 0 |

For this all-expired fixture, the fast-path median is **8.902 microseconds** versus
**275.872 microseconds** for per-entry heap removal and **63.568 microseconds**
for the scan. Setup creates a full cache with distinct 1 ms-spaced deadlines in
a fixed non-monotonic permutation. No maximum entry has been removed, so the
upper bound is exact; observation is at that maximum, making all 1,024 entries
provably expired. Signing, hash/size warming, store allocation, refill and
postcondition checks are excluded. Only the locked prune operation is timed.
The first fast prune sample reported **1 B/op**; its other two reported zero,
so the median is zero. The raw value is retained rather than replaced.

The prepared rejection check is an unfavorable result: **42.38 to 46.82 ns/op**
from heap to fast, about **10.5% higher median time**. Fast rejection samples were
41.16, 46.82 and **65.10 ns/op**, and none was discarded. Prepared duplicate
medians were 42.46 and 38.35 ns/op. Three samples with this variability do not
prove a general no-regression claim or identify why a nanosecond-scale difference
occurred. In particular, the no-expiry path checks the heap root and returns
before the upper-bound test. The measurements are reported without retuning.

Several important boundaries remain:

- **Partial expiry still uses O(K log N) heap removals.** One future live entry
  prevents bulk clear. A conservatively stale upper bound after removing a former
  maximum can also miss the fast path even when every remaining entry is due.
  That is safe but can retain the old bulk-removal cost; this batch did not time
  either fallback workload. Continuous admissions may keep a future deadline,
  so the all-expired result is not representative of all ongoing traffic.
- This is **store reference release**, not completed memory reclamation. The
  benchmark deliberately retains its transaction fixtures across iterations.
  Queue/in-flight references can also retain transactions in real operation.
  GC completion, RSS reduction and peak/retained heap memory were not measured.
- **The separately allocated entry remains.** This change adds one timestamp to
  the store, not an entry pool. The historical prepared add/remove case measured
  80 B/op and 1 alloc/op, and the original indexed refill cohort measured 1,024
  entry allocations per operation. These cases were not rerun here; their cost
  has not been claimed solved, removed or remeasured.
- Both hot paths reuse signed objects with cached hashes/sizes. The rejection
  case repeatedly offers the **same absent hash**. They exclude signing,
  validation, HTTP, peers and transport, and are **not new-transaction TPS**.
- Reported bytes/allocations are benchmark per-operation averages, not a process
  memory budget. Integer allocs/op output may hide fractional averages. The
  current samples are not merged into the old 42-sample series; three short
  samples per case do not establish p95/p99, a confidence interval or long-run
  behavior. The scan's 123.544 microsecond first prune sample remains included.

Defaults, retained-admission semantics and the ideal count-retention upper bound
`1024 / 120 = 8.533...` new cache lifetimes/s are unchanged. No production-readiness,
network throughput, tail-latency or new capacity guarantee follows from this batch.

## All original samples

Each section is in actual execution order; each raw result is reproduced once.
Prune uses exactly 100 iterations. The two prepared cases use one-second
calibration, and `-2` denotes GOMAXPROCS=2.

### Round 1, scan

```text
BenchmarkStorePruneExpired-2   	     100	    123544 ns/op	      1024 entries/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2         	   75542	     15699 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2   	   70299	     15386 ns/op	       0 B/op	       0 allocs/op
```

### Round 1, heap

```text
BenchmarkStorePruneExpired-2   	     100	    275872 ns/op	      1024 entries/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2         	30748678	        38.22 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2   	29153247	        42.25 ns/op	       0 B/op	       0 allocs/op
```

### Round 1, fast

```text
BenchmarkStorePruneExpired-2   	     100	      8552 ns/op	      1024 entries/op	       1 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2         	30837312	        37.90 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2   	27022371	        41.16 ns/op	       0 B/op	       0 allocs/op
```

### Round 2, heap

```text
BenchmarkStorePruneExpired-2   	     100	    293282 ns/op	      1024 entries/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2         	26435948	        42.46 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2   	25114572	        47.06 ns/op	       0 B/op	       0 allocs/op
```

### Round 2, fast

```text
BenchmarkStorePruneExpired-2   	     100	     13485 ns/op	      1024 entries/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2         	25000532	        40.52 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2   	31005520	        46.82 ns/op	       0 B/op	       0 allocs/op
```

### Round 2, scan

```text
BenchmarkStorePruneExpired-2   	     100	     63568 ns/op	      1024 entries/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2         	   80271	     15061 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2   	   73772	     14921 ns/op	       0 B/op	       0 allocs/op
```

### Round 3, fast

```text
BenchmarkStorePruneExpired-2   	     100	      8902 ns/op	      1024 entries/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2         	34774686	        38.35 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2   	25326861	        65.10 ns/op	       0 B/op	       0 allocs/op
```

### Round 3, scan

```text
BenchmarkStorePruneExpired-2   	     100	     49841 ns/op	      1024 entries/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2         	   67177	     15808 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2   	   77484	     15126 ns/op	       0 B/op	       0 allocs/op
```

### Round 3, heap

```text
BenchmarkStorePruneExpired-2   	     100	    250097 ns/op	      1024 entries/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2         	24824248	        43.46 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2   	28281716	        42.38 ns/op	       0 B/op	       0 allocs/op
```
