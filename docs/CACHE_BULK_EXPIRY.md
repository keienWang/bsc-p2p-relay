# Conservative all-expired cache cleanup experiment

Status: **NOT PRODUCTION READY**. Candidate and independent relay race runs, one
narrow three-implementation benchmark batch, and the later integrated local
default/official-txpool checks passed. Exact hosted CI and operational release
gates remain separate; see the integration record below.
This narrow follow-up starts from local commit
`df9e5c299da3a6a65ba81cff5137c74414cf6a8e`. The previous indexed implementation,
scan baseline and [42 historical benchmark samples](CACHE_EXPIRY_BENCHMARK_RESULTS_2026-11-10.md)
are preserved unchanged. Those measurements are not evidence for this experiment.

## Motivation and scope

The earlier independent 1,024-entry staggered-expiry benchmark measured a median
257,926 ns for indexed heap removal versus 48,782 ns for the scan, approximately
5.29 times slower. Its combined refill/prune measurement hid that cost because
the old refill itself scanned the cache on every insertion. This experiment
adds one conservative upper-bound timestamp, not a second index or a new
eviction policy, to avoid repeated heap repair when every cached entry is
provably due.

Defaults remain 1,024 entries, 16 MiB serialized bytes and 120 seconds. Validation,
count/byte rejection, original admission deadlines, retries, successful-write
retention, targeted get, queue rollback and lock order do not change. No pooling,
new dependency, background work, network behavior, persistence or API is added.
The existing separately allocated entry still costs 80 B/op and 1 alloc/op in
the historical prepared add/remove measurement. This experiment does not remove
that allocation or establish a new allocation/RSS result.

## Invariant and proof

Under the existing cache lock, `expiryUpperBound` obeys:

1. For every current entry, `entry.expires <= expiryUpperBound`.
2. An empty cache has a zero upper bound. First insertion assigns its actual
   deadline, even if that timestamp is before the zero `time.Time` value.
3. While nonempty, insertion only raises the bound when its deadline is later.
   Duplicate and rejected insertions do not change it.
4. Removing an entry can leave a later stale upper bound. It cannot make the
   bound earlier than a remaining deadline. Removing the final entry resets it.

The induction is direct: first insertion establishes equality; subsequent
insertions take a maximum; removing members preserves the inequality. A stale
maximum therefore only makes the fast path less likely, never permits removal
of a live entry. It does not assume increasing insertion times or FIFO expiry.

Pruning still checks the minimum deadline first. If the cache is empty or that
deadline is in the future, it returns without checking the new bound. Otherwise,
only `now >= expiryUpperBound` permits bulk cleanup. Together with invariant 1,
this proves `now >= entry.expires` for **every** entry, including exact equality.
If that proof does not hold, the existing per-entry heap removal path runs.
An unrelated future live entry prevents the bulk path. A deleted former maximum
can leave an overly late bound, causing ordinary removal even when all remaining
entries happen to be due; that is intentional conservative behavior.

Bulk cleanup marks every live heap entry's index -1, clears the map and live
heap slots, resets heap length and serialized bytes, and resets the bound. The
heap's bounded backing capacity remains reusable. Spare slots were already nil
under the existing invariant; clearing all live slots leaves no cached entry or
transaction reference anywhere in the backing array. The map is cleared rather
than replaced. This avoids per-entry heap repairs; its work includes a traversal
of live heap entries and the runtime's map clear, whose cost depends on retained
map storage. It is not a promise of measured latency, particularly at different
occupancies or map high-water marks.

`get` does not call bulk cleanup and continues to remove only its expired target.
Explicit remove/rollback likewise stays targeted. All paths that empty the
cache reset the bound, so remove/reinsert cannot inherit a previous lifetime's
upper bound when starting from empty. No removed entry is pooled or reused.

## Regression coverage

The existing test-only expiry-rewrite helper now rebuilds a valid upper bound
under the cache lock after its heap fix/rebuild. This is not a production API.
Its five original negative fixtures retain their purposes and assertions.
The shared invariant checker now checks every entry against the upper bound and
requires an empty store to have a zero bound. Thus the existing 24,000-step
deterministic scan-reference differential test, concurrency checks and load/
unique-capacity bounds also validate the new invariant.

Explicit regressions cover:

- one future live entry blocking bulk cleanup while due entries are removed;
- deleting the maximum leaving a stale high bound, preserving a remaining live
  entry and eventually completing fallback expiry with a reset bound;
- non-monotonic insertion times and equality at the largest deadline;
- full cleanup of map, heap, bytes, removed indices and every backing-array slot;
- a fresh insertion after bulk cleanup, including earlier timestamps and dates
  before the zero `time.Time`, without stale-bound inheritance;
- get remaining target-only even if all entries are expired, and same-hash
  remove/reinsert with empty-store bound reset;
- test-helper deadline extension preserving a live entry and all-entry deadline
  shortening maintaining a valid upper bound.

## Verification and measurement boundary

Local commit `6109ca14516a3c251cffcda741558c048241adf9` passed one complete
untagged relay race attempt on 2026-11-10, Go 1.26.10 linux/amd64:

```sh
GOMAXPROCS=2 GOFLAGS='-buildvcs=false -p=1' \
  timeout -s KILL 150s go test -race -count=1 -timeout=120s -v ./internal/relay
```

The actual outer timeout used `timeout -s KILL 150s`, with Go's 120-second
timeout inside it. Every `RELAY_SOAK*` and `RELAY_UNIQUE*` environment variable
was unset. Exit code **0**, package **3.858s**, approximately eleven seconds
total wall time including compilation/startup. The new five bulk-expiry tests,
24,000-step scan-reference differential and all eight short unique-capacity
scenarios passed, along with the prior relay regressions and fuzz seeds. Long
soak, intentional failure probe and 150-second default-TTL experiment were
skipped. There was no failed attempt or retry.

The complete first-attempt log is
`01-full-relay-race-20261110T190454Z-2.log` (SHA-256
`00baec7b9eedb015cde185e61a3d7977b20379cf07af3f873b45d4e5b52df28d`), retaining
command, source SHA values, environment, output and exit code.

A later approved **single** three-implementation batch compared the unchanged
prune-only benchmark and two prepared duplicate/rejection checks. All 18 commands
exited 0. The [new 27-sample report](CACHE_BULK_EXPIRY_RESULTS_2026-11-10.md) records
every sample, median, source and limit. All-expired pruning improved in that
fixture, while the prepared rejection median was 10.5% higher than the original
heap. There is no blanket no-regression claim. Partial expiry and stale-upper-
bound fallback retain O(K log N) behavior and were not timed in this batch.
The earlier complete twelve-command/42-sample batch was not rerun or modified.

This measures release of store references, not completed GC or RSS reduction;
the benchmark's transaction fixtures remain alive. The 80-byte separately
allocated-entry cost has not been removed. At the initial candidate stage,
full-repository/tagged tests and release/security/operational gates had not been
rerun. The subsequent local integration checks below supersede only that local
test status, not the open operational gates. No production-readiness or
new-throughput claim is made. The ideal default
count-retention upper bound remains `1024 / 120 = 8.533...` new lifetimes/s.


## Independent review and integrated local validation

The clean source `9016a1596fcb3289de1283a2e82952bf9a5b7f8a` was independently
checked without editing it. Runtime and tests matched the measured `6109ca1`
byte for byte. One complete relay race run exited 0 in **3.845s**, including the
five bulk-expiry groups, 24,000 differential transitions and all eight short
unique-capacity scenarios (**1.70s**). The 18 original command logs, all 27 raw
samples, their medians and source fingerprints were independently reconciled
with the report; no samples were rerun or discarded. The earlier 42-sample
report remained unchanged. The relay race log SHA-256 is
`4e787bc3bc0b63ea36979d71642ccca64975001976c0b08e15633d8e90553c19`.

Local integration commit **`2dd88ac3cbbf09ed3234173139ff667c9bb63c17`** applied
all thirteen cache paths byte for byte to `3d6d51b`, whose published equivalent
is `1baeb6f33d1915638c693aed5be61f543f03437d`. All other 111 baseline paths,
including all three CI jobs, canonical-isolation research, the official-txpool
module, prior evidence and the pinned-source SBOM, were preserved byte for byte.
The integration tree is `2a09a7d97087343b9390ec79f6a993f108052b5b`.
The later documentation commit changes no executed code or test fixture.

On 2026-11-10 **19:15:56–19:19:45 UTC**, one serial local validation batch
completed with all six phases and the batch exiting 0, without retries. It used
Go 1.26.10 linux/amd64, `GOMAXPROCS=2`, `GOFLAGS='-buildvcs=false -p=1'`,
`GOTOOLCHAIN=local`, `GOWORK=off`, `GOENV=off`, and no `RELAY_SOAK*` or
`RELAY_UNIQUE*` opt-in variables:

1. `make check smoke vuln`: vet; complete default race suite; build; 10 Python
   tests; gofmt; module verification; command/default-test dependency guard;
   process smoke; default application scan. The race suite reported checkpoint
   **41.251s**, compatibility **1.144s**, evidence **1.085s**, first epoch
   **40.665s**, and relay **3.867s**. Smoke covered startup, health, readiness,
   malformed input, shutdown and no data files. Scanner: **No vulnerabilities
   found**.
2. Pinned `govulncheck v1.9.0 -test ./...`: **No vulnerabilities found**.
3. Official-txpool tagged dependency guard: passed.
4. `go vet ./internal/relay` using the official-txpool alternate module: passed.
5. `go test -race -count=1 -timeout=180s ./internal/relay` using that alternate
   module and tag: passed in **39.149s**. This includes the isolated official
   handler/fetcher/txpool loopback regression, not a public-network node test.
6. Pinned `govulncheck v1.9.0 -test -tags=officialtxpool -show=verbose
   ./internal/relay` using the same alternate module: **No vulnerabilities
   found**.

Phases 3–6 used
`GOFLAGS='-buildvcs=false -p=1 -mod=readonly -modfile=integration/officialtxpool/go.mod -tags=officialtxpool'`.
Each phase had a finite outer timeout (480/180/90/120/210/180 seconds), with the
existing 300-second default package timeout and 180-second tagged package timeout.
No timeout fired. Complete logs retain commands, output, timestamps and exits:

| Log | SHA-256 |
| --- | --- |
| `01-default.log` | `b5b96016a677f173f22e634627967688515afe75fbce8832e445d5ea0ec86516` |
| `02-default-test-scan.log` | `c77c47d4ef512221cae31ae7a46cafae45e789005c55cc090ddf54695fd97c37` |
| `03-official-guard.log` | `040215fc7fbeb4354f508530ba08ae60dbea548d4145219e7f5bdaf26f092d58` |
| `04-official-vet.log` | `5b784eff5729fb4d00bbb76d0ced1f42c1b14ae0e89e1d2114a3d1e34ad0a47b` |
| `05-official-race.log` | `cca4c57b9693d19615dbee88ff803e66c20ca46b8a023bca6f465c6a85483d42` |
| `06-official-scan.log` | `a99458c2a65fa69db1be7016e8190213fcdad521a7cdcfd241a5c01a1ff10452` |

The canonical-isolation implementation and its third CI job were unchanged and
were **not rerun locally in this batch**. Its previously verified public baseline
CI is not evidence for the new cache commit; the exact eventual public commit
must pass all three hosted jobs. No new timed fuzz campaign, long/default-TTL
soak, benchmark rerun, public-network test, release binary distribution or SBOM
source refresh occurred. Existing SBOM metadata continues to describe its stated
older public source. The known BLS concurrency diagnostic remains unresolved and
excluded from ordinary passing tests; a clean vulnerability scan does not resolve
it. Live authenticated heads, receipts, finality and production acceptance remain
open.
