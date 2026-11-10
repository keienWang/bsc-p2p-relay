# Cache expiry index: paired local measurements

Status: **NOT PRODUCTION READY**. These are cache microbenchmarks, not HTTP,
network, remote txpool acceptance, production TPS, or an end-to-end latency SLO.
The [design and verification notes](CACHE_EXPIRY_INDEX.md) describe the unchanged
admission/retention contract and what each benchmark includes.

## Source and method

- Scan baseline: `683a9e5a56dd4c890234d3e02aa1125c66bb49a4`, tree
  `7d4e2f3a41ea20a28707a9e4cb7fe3e547d68339`. A separate worktree had no tracked
  changes and only the new `internal/relay/store_bench_test.go` file. It ran the
  actual baseline `store.go`, not the differential-test reference implementation.
- Indexed candidate: `e43e919beed9c78204901dd93684cee5d57843ab`, tree
  `2e0a6175a307ffe4ff0cd456f252fd3b2822df59`, clean before and after measurement.
  Runtime implementation is unchanged from `969cf4f52563ce9f328330d7d89bf871bdb9bfc2`.
- The exact benchmark file was identical on both sides: SHA-256
  `50bae83e7bbb462179f6ee608e054a870481644602861aaecdc444ae93921283`.
  Other fixture helpers came from the same baseline. No new dependency was used.
- Go 1.26.10, linux/amd64, `INTEL(R) XEON(R) PLATINUM 8573C`, `GOMAXPROCS=2`,
  `GOFLAGS='-buildvcs=false -p=1'`. CPU identification comes from Go benchmark
  output. Optional `lscpu` metadata failed because the container lacked
  `/sys/devices/system/cpu/possible`; the warning was retained without retry.
- One coordinated batch, 2026-11-10 18:50:47–18:52:20 UTC: **93 seconds**.
  Three rounds ordered **old→new, new→old, old→new**. Each side ran the six cache
  cases first, then isolated prune. All **12 commands exited 0**, batch exit 0.
  No failed benchmark, retry, discarded sample or post-result parameter change.
- Every command had a 40-second Go timeout and 45-second outer hard timeout;
  the entire batch had a 240-second hard limit. Long soak, intentional failure
  probe and the 150-second default-TTL experiment remained disabled. Race
  instrumentation was disabled for timings. No other project heavy job was
  deliberately run concurrently; this is not a dedicated or CPU-pinned host.

Each side in each round used these commands:

```sh
timeout -s KILL 45s go test -timeout=40s ./internal/relay -run='^$' \
  -bench='^BenchmarkStoreCache$' -benchmem -benchtime=1s -count=1
timeout -s KILL 45s go test -timeout=40s ./internal/relay -run='^$' \
  -bench='^BenchmarkStorePruneExpired$' -benchmem -benchtime=100x -count=1
```

The complete retained batch log is `03-paired-batch.log` (SHA-256
`b476f160f155967d9944aa6c7f378b37b3e5c6a9f4da5404b65674f01f5fafff`); its twelve
individual command logs preserve timestamps, command lines, output and exits.
`03-paired-summary.json` preserves parsed samples and medians (SHA-256
`abc80dcdee3d29699c6a357c0bcfbbf04f701cb1ae6a28cb1f25aa7eae24f284`). All raw
benchmark result lines are also reproduced below so the samples remain visible
with the source documentation.

## Medians, including costs

Each median uses all three samples. Units are per benchmark operation. The two
cohort rows each operate on **1,024 entries**; they are not per-entry timings.

| Case | Scan ns/op | Indexed ns/op | Scan B/op | Indexed B/op | Scan allocs/op | Indexed allocs/op |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 900 live, duplicate prepared | 14,813 | 38.61 | 0 | 0 | 0 | 0 |
| 1,024 live, duplicate prepared | 16,019 | 38.78 | 0 | 0 | 0 | 0 |
| 1,024 live, same absent prepared hash rejected | 16,489 | 41.28 | 0 | 0 | 0 | 0 |
| 900 live, prepared add + rollback remove | 14,123 | 181.8 | 0 | 80 | 0 | 1 |
| 1,024 prepared refill + equal-deadline prune | 10,030,001 | 208,963 | 6,528 | 82,060 | 0 | 1,024 |
| 1,024 live, new signed hash constructed + rejected | 52,287 | 35,759 | 1,800 | 1,800 | 46 | 46 |
| 1,024 staggered expiries, prune only | 48,782 | 257,926 | 0 | 1 | 0 | 0 |

The no-expiry prepared duplicate/rejection cases improve substantially because
they no longer scan 900/1,024 live map entries. These cases reuse already signed
objects with cached hashes/sizes and exclude signature validation, HTTP and all
peer work. They must not be described as fresh transaction throughput. The
new-signed-hash case includes construction/signing and rejects every offer; its
median falls from 52.287 to 35.759 microseconds, a much smaller change.

The independent prune case exposes a regression: **48.782 to 257.926 microseconds
per 1,024-entry cohort**, approximately **5.29× slower**. Its raw indexed samples
are 269.844, 246.325 and 257.926 microseconds versus 55.925, 46.408 and 48.782 for
the scan. Its median reported allocation is **1 B/op, 0 allocs/op**, not silently
rounded down to zero here. The combined refill/prune case is faster overall, but
the old O(N²) refill work can hide this O(N log N) reclamation trade-off. Also,
the combined case uses equal deadlines while prune-only deliberately staggers
them, so these are different workloads and their timings must not be subtracted.

Allocation cost also increases: prepared add/remove changes from **0 B/op and
0 allocs/op to 80 B/op and 1 alloc/op**; the refill cohort reports **82,060 B/op
and 1,024 allocs/op**, versus 6,528 B/op and 0 reported allocs/op for the scan.
The heap uses separately allocated live entries where the old map stored values.
These measured allocations are not net retained-memory or RSS measurements:
map layout, backing arrays, initial growth, GC and transaction references also
matter. Go's integer allocs/op output can show zero for a fractional average;
that does not prove no individual allocation occurred. All raw B/op values are
retained below, including the small prune-only values.

The index trades lower no-expiry admission overhead for per-admission allocation
and slower bulk expiry reclamation while holding the cache lock. Three short
samples do not establish tail latency, long-duration GC behavior or a production
resource budget. This measurement does not justify increasing defaults, evicting
live accepted entries, shortening TTL, or claiming a throughput SLO. The default
count-retention constraint remains `1024 / 120 = 8.533...` new cache lifetimes/s.
No HTTP/transport or public-network throughput extrapolation is made.

## All original result samples

Result lines below are copied verbatim from the individual logs. Each cache
case uses calibrated one-second timing; prune-only uses exactly 100 iterations.
`-2` denotes GOMAXPROCS=2, not two independent measurements. No sample is omitted.

### Round 1, scan baseline

```text
BenchmarkStoreCache/near_900_duplicate_prepared-2         	   87010	     14813 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2        	   78604	     14774 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2  	   64965	     19847 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/near_900_add_remove_prepared-2        	   81730	     13748 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/expiry_cohort_1024_refill_and_prune_prepared-2         	     126	   9470008 ns/op	      1024 entries/op	    5181 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_unique_signed_in_loop-2             	   23798	     49541 ns/op	    1800 B/op	      46 allocs/op
BenchmarkStorePruneExpired-2   	     100	     55925 ns/op	      1024 entries/op	       0 B/op	       0 allocs/op
```

### Round 1, indexed candidate

```text
BenchmarkStoreCache/near_900_duplicate_prepared-2         	31392837	        39.54 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2        	31946761	        46.30 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2  	29443701	        39.82 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/near_900_add_remove_prepared-2        	 5947143	       181.8 ns/op	      80 B/op	       1 allocs/op
BenchmarkStoreCache/expiry_cohort_1024_refill_and_prune_prepared-2         	    7076	    198601 ns/op	      1024 entries/op	   82033 B/op	    1024 allocs/op
BenchmarkStoreCache/full_1024_rejected_unique_signed_in_loop-2             	   31302	     35701 ns/op	    1800 B/op	      46 allocs/op
BenchmarkStorePruneExpired-2   	     100	    269844 ns/op	      1024 entries/op	       1 B/op	       0 allocs/op
```

### Round 2, indexed candidate

```text
BenchmarkStoreCache/near_900_duplicate_prepared-2         	35154769	        36.62 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2        	29516412	        38.78 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2  	27391108	        41.28 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/near_900_add_remove_prepared-2        	 5817418	       179.3 ns/op	      80 B/op	       1 allocs/op
BenchmarkStoreCache/expiry_cohort_1024_refill_and_prune_prepared-2         	    3543	    298067 ns/op	      1024 entries/op	   82145 B/op	    1024 allocs/op
BenchmarkStoreCache/full_1024_rejected_unique_signed_in_loop-2             	   36756	     35759 ns/op	    1800 B/op	      46 allocs/op
BenchmarkStorePruneExpired-2   	     100	    246325 ns/op	      1024 entries/op	       0 B/op	       0 allocs/op
```

### Round 2, scan baseline

```text
BenchmarkStoreCache/near_900_duplicate_prepared-2         	   78907	     15926 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2        	   72340	     16298 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2  	   80466	     16489 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/near_900_add_remove_prepared-2        	   80408	     15934 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/expiry_cohort_1024_refill_and_prune_prepared-2         	     100	  10731787 ns/op	      1024 entries/op	    6528 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_unique_signed_in_loop-2             	   21975	     56979 ns/op	    1800 B/op	      46 allocs/op
BenchmarkStorePruneExpired-2   	     100	     46408 ns/op	      1024 entries/op	       0 B/op	       0 allocs/op
```

### Round 3, scan baseline

```text
BenchmarkStoreCache/near_900_duplicate_prepared-2         	   78700	     14623 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2        	   74696	     16019 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2  	   67935	     16304 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/near_900_add_remove_prepared-2        	   84112	     14123 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/expiry_cohort_1024_refill_and_prune_prepared-2         	     100	  10030001 ns/op	      1024 entries/op	    6528 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_unique_signed_in_loop-2             	   23155	     52287 ns/op	    1800 B/op	      46 allocs/op
BenchmarkStorePruneExpired-2   	     100	     48782 ns/op	      1024 entries/op	       0 B/op	       0 allocs/op
```

### Round 3, indexed candidate

```text
BenchmarkStoreCache/near_900_duplicate_prepared-2         	28097008	        38.61 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_duplicate_prepared-2        	29414232	        37.63 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/full_1024_rejected_absent_prepared-2  	28568744	        42.28 ns/op	       0 B/op	       0 allocs/op
BenchmarkStoreCache/near_900_add_remove_prepared-2        	 5740293	       211.5 ns/op	      80 B/op	       1 allocs/op
BenchmarkStoreCache/expiry_cohort_1024_refill_and_prune_prepared-2         	    5715	    208963 ns/op	      1024 entries/op	   82060 B/op	    1024 allocs/op
BenchmarkStoreCache/full_1024_rejected_unique_signed_in_loop-2             	   31572	     38026 ns/op	    1800 B/op	      46 allocs/op
BenchmarkStorePruneExpired-2   	     100	    257926 ns/op	      1024 entries/op	       1 B/op	       0 allocs/op
```
