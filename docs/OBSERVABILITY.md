# Local submission statistics

Status: experimental broadcast-core observation, **not production readiness**,
remote acceptance, transaction execution or chain confirmation.

`GET /v1/stats` is a read-only endpoint on the existing loopback HTTP listener.
It uses the same Host/Origin guards and JSON, `Cache-Control: no-store` and
`X-Content-Type-Options: nosniff` headers as the other endpoints. GET returns 200
even without ready peers; other methods return the existing GET-required 405.
Health, readiness, transaction response and error JSON contracts are unchanged.

## Frozen version 1 schema

All fields are required. Counters are unsigned 64-bit JSON integers; consumers
must retain integer precision, including values above 2^53. No field contains
transaction bytes, transaction hashes, senders or peer identities.

<!-- stats-metadata:start -->
| Field | Type and meaning |
| --- | --- |
| `schema_version` | Integer 1, permanently fixed for this endpoint. |
| `instance_id` | 32 lowercase hex characters representing an independent random 128-bit identity for this Node. Not derived from P2P keys or host identity; not an authentication credential. |
| `created_at` | UTC RFC3339Nano Node-construction time; descriptive, not proof of unique identity or process start. |
| `observed_at` | UTC RFC3339Nano time when snapshot collection begins, not a global atomic instant. |
| `submit` | Object containing the eight counters below. |
| `http` | Object containing the separate HTTP counter below. |
<!-- stats-metadata:end -->

<!-- stats-submit:start -->
| Field | Counted normal Submit return |
| --- | --- |
| `accepted_new_cache_lifetime_total` | Nil error after a fresh insertion in this attempt. |
| `accepted_cached_retry_total` | Nil error after a live cache hit, including new peer fanout and success with no new queue item. |
| `rejected_validation_total` | Stateless transaction validation failed. |
| `rejected_no_peers_total` | ErrNoPeers. |
| `rejected_cache_full_total` | ErrCacheFull; count and byte limits remain combined. |
| `rejected_queue_unavailable_total` | ErrQueuesFull; closing sessions or sent-map bounds can also cause this, not only physically full queues. |
| `rejected_admission_expired_total` | ErrAdmissionExpired. |
| `other_total` | Any other normal returned classification; expected zero for current supported paths, never implicitly success. |
<!-- stats-submit:end -->

<!-- stats-http:start -->
| Field | Meaning |
| --- | --- |
| `concurrency_rejected_total` | Existing transaction-handler eight-slot limiter selected its 429 rejection branch, before Submit. A response decision, not proof the client received it. |
<!-- stats-http:end -->

The version-1 fields, types and meanings are frozen. Future schema changes use a
separate `/v2` endpoint while preserving `/v1/stats`; v1 does not silently change
its schema version or field set. JSON object key order is not a contract.

## Denominators and retention

Each Submit counter event is one normally completed `Node.Submit` call, including
direct callers, not necessarily one HTTP request. A panicking or not-yet-returned
call records no normal outcome. There are no started/in-flight counters or new
panic-recovery behavior. A process crash can lose in-memory counters and queues.

Fresh fanout failure and rollback count only a rejection. A cached retry rejection
does not retract an earlier acceptance. Partial fanout is one successful Submit,
not one success per peer. Neither success nor retries renew the cache deadline.
A fresh lifetime may already have expired by return: the existing deadline gate
is before fanout, not a new live-at-return guarantee.

The same bytes may count as a new lifetime after expiry, rollback or restart.
Accepted new cache lifetimes/s is not offered distinct hashes/s or transaction TPS.
HTTP body/hex/media/method/Host/Origin rejections do not call Submit; HTTP concurrency
refusals are separately counted. Do not add that counter to the Submit denominator
or call the sum complete HTTP traffic. This API has no HTTP request denominator.

`Submission.queued_peers` remains a per-request local queue-item count. Existing
Health `written_messages` counts writer messages, including protocol replies, not
transactions, acknowledgments or confirmations. No queue, expiry, abandonment,
fresh-byte or delivery statistics are added here. Do not infer those events by
subtracting Submit outcomes from message writes.

## Collection and unavailable observations

For example, with an already reviewed isolated local process:

```sh
curl --noproxy '*' --connect-timeout 2 --max-time 5 -i http://127.0.0.1:8787/v1/stats
```

A non-200 response (including 404 on older binaries), timeout, truncated or invalid
JSON, missing field, invalid field type or unknown schema is **unavailable data**.
Do not replace it with zeroes or present the last sample as a fresh observation.

Only compare sequential, non-overlapping scrapes of the same endpoint and the
same `instance_id`. Use caller monotonic elapsed time for approximate counter
rates; `created_at` and `observed_at` are descriptive wall-clock times. New instance
identity or a decreasing counter invalidates a delta. uint64 counters can wrap;
do not report a wrapped delta as activity. Serial scrapes avoid response-reordering
ambiguity but do not make the counter snapshot atomic.

Individual atomic loads are race-safe, but the eight fields may span different
moments. Sum-of-outcomes equality with a test's known normal-return count is valid
only after joining callers and reaching quiescence. There is no global transaction,
HTTP or transport accounting identity. No threshold, alert duration or SLO is
certified by these counters.

## Resource and security boundaries

Metric state and snapshot work are fixed O(1). A stats request takes no submission,
cache or peer lock, performs no cache prune, and occupies none of the eight
transaction slots. Slots remain independently scoped to each Handler; all Handlers
of a Node share the same counters and instance identity. There is no lazy metadata
initialization. A new Node obtains new random identity during construction;
identity generation failure fails construction rather than substituting zeroes
or timestamps.

This is not a claim that total HTTP resource use is bounded: the new GET endpoint
has no additional rate limiter, and existing HTTP timeouts still apply. Do not
expose it through a public proxy, tunnel or port forward. Local Host/Origin guards
are not authentication or isolation from other host users/processes. There is no
metrics exporter, per-hash history, identifying metric label, reset endpoint,
resource sampler, histogram or additional background goroutine. Existing cache
limits and all [production gates](PRODUCTION_READINESS.md) remain unchanged.

## Offline snapshot deltas

Save two successful response **bodies** from sequential, non-overlapping scrapes
of the same reviewed loopback endpoint. For example, before and after a bounded
observation interval (do not use `-i`, which also saves HTTP headers):

```sh
curl --noproxy '*' --fail --connect-timeout 2 --max-time 5 \
  http://127.0.0.1:8787/v1/stats > before.json
# Later, after the first request has completed:
curl --noproxy '*' --fail --connect-timeout 2 --max-time 5 \
  http://127.0.0.1:8787/v1/stats > after.json
python3 scripts/stats_delta.py before.json after.json
```

Check both curl exit statuses; do not treat a failed collection as a snapshot.
The standard-library-only comparison tool makes no network requests and reads
at most 16 KiB plus one byte per file. It validates the frozen v1 schema using
the smoke validator, preserves uint64 integer precision, and rejects changed
instance/creation metadata, any decreasing counter, malformed/truncated JSON,
duplicate fields, and oversized snapshots. Failure exits 1, writes a diagnostic
to stderr and emits no result to stdout; it never substitutes zero counters.

`submit_delta` and `http_delta` retain their original counter field names but
contain differences, not cumulative totals. They remain separate denominators.
The output retains both wall-clock observation timestamps for context but does
not compute rates, a combined total, throughput, delivery, or success percentages.
Wall clocks can move backward. The tool cannot prove that the caller supplied
ordered samples from the same endpoint, detect intervening restarts with forged
metadata, or make snapshots globally atomic. Counter wrap invalidates a delta
rather than being interpreted as activity. All observation limitations above
still apply; the tool is not an exporter, monitor, or production readiness gate.

### Capture without partial baseline files

For a new file rather than shell redirection, the optional one-shot collector
validates the response before publishing it:

```sh
python3 scripts/capture_stats.py 127.0.0.1:8787 before.json
# After the first command succeeds and the observation interval has elapsed:
python3 scripts/capture_stats.py 127.0.0.1:8787 after.json
python3 scripts/stats_delta.py before.json after.json
```

Only literal loopback IPs with explicit ports are accepted (`[::1]:8787` works);
hostnames, public addresses, URL paths, credentials and query strings are rejected.
The collector makes one GET to `/v1/stats`, ignores environment HTTP proxies and
does not follow redirects. It reuses the smoke reader's status/header/schema,
framing and 16 KiB response checks, with its two-second socket idle timeout.
For an optional total limit, use `--fetch-deadline 5`. On platforms with POSIX
interval timers this bounds the connection, response headers, body and snapshot
validation for one fetch, including slow responses that stay below the idle
timeout. The value must be finite and between 0.001 and 60 seconds. The option
refuses to use an occupied process alarm and does not time baseline-file reads
or publication. A fetch deadline or idle timeout leaves no snapshot. Without
the option, the original idle timeout remains the only network timer. The
collector is not a polling daemon.

The output directory must already exist and support same-directory hard links.
The validated JSON is written to a temporary file and atomically published as a
**new** filename; an existing baseline is never overwritten. Failures return 1
and no success message. Invalid HTTP/JSON observations never produce a baseline
file. Keep checking exit statuses and keep collections sequential; this wrapper
does not change the counter/delta interpretation or authorize public exposure.

To append a capture to an existing instance's observation window, use:

```sh
python3 scripts/capture_stats.py 127.0.0.1:8787 after.json --same-instance-as before.json
```

The collector validates the saved baseline before fetching. Baselines and
comparison inputs must be regular files (symlinks to regular files are allowed),
not pipes or devices. This is a file-type check, not a filesystem I/O deadline.
The collector writes a new
snapshot only if both `instance_id` and `created_at` match. After a restart,
start a new baseline instead of treating a new process as a continuation.
This is a local correlation check, not authentication or a counter-delta check;
use `stats_delta.py` to compare validated snapshots. Ordinary capture without
the option is unchanged.

### Collector/comparator command-line regression

`python3 -m unittest discover -s scripts -p 'test_observation_pipeline.py'`
exercises the capture and comparison scripts as separate processes from a fresh
working directory. A loopback-only HTTP fixture supplies snapshots; the check
covers file interchange, exact uint64 deltas and nanosecond metadata, rejected
restart/reset comparisons, and preservation of an existing baseline on a failed
repeat capture. It runs in the ordinary Python test suite. This fixture validates
the operator-tool pipeline, not live peer readiness, throughput, or delivery.

To keep a comparison as an artifact without shell redirection truncating an
existing report, use:

```sh
python3 scripts/stats_delta.py before.json after.json --output delta.json
```

The optional output is published only after both snapshots and the comparison
pass validation. It uses the collector's same-directory temporary-file and
atomic no-clobber publication: an existing report (or an input snapshot used as
output) is preserved, failures exit nonzero, and temporary files are cleaned up.
The destination directory must already exist and support hard links. This is
atomic visibility, not a promise of directory-entry durability after power loss.
Without `--output`, the command still emits the JSON comparison to stdout.
Failure messages identify the affected stage: before snapshot, after snapshot,
comparison, or output report. A decreasing counter reports its field and exact
before/after values so an operator can check the baseline without interpreting
a reset or reversed ordering as a valid delta.

### Assert sampled counters did not change

```sh
python3 scripts/stats_delta.py before.json after.json --require-unchanged
```

Use this optional check when the expectation is that every tracked counter
stays unchanged, for example across an isolated observation-only test window.
Exit `0` means the snapshots compared and all deltas were zero; exit `3` means
they compared and at least one tracked counter increased. Both produce the
complete comparison, including with `--output`. Invalid comparisons or output
publication failures still exit `1`, and argument usage errors exit `2`.
Without the flag, valid comparisons still exit `0` regardless of deltas.
Zero sampled deltas do not prove that the relay, peers, or chain were idle;
only the counters in this schema were checked.

### Readiness scope and polling cost

`/readyz` reports whether any connected session negotiated transaction receive
capability. It can remain HTTP 200 with a full local cache or full outgoing
queue: it is not an admission-capacity check, remote acceptance, or confirmation.
`/healthz` remains HTTP 200 when no such session exists. Both handler modes use
this same rule. `TestHealthReadinessCapabilityAndCapacity` guards these semantics
across capability transitions with full synthetic cache/queue fixtures and
verifies polling does not submit work or drain queues.

For a one-shot local readiness check with distinct shell exit statuses:

```sh
python3 scripts/probe_readiness.py 127.0.0.1:8787 --expect-network mainnet --fetch-deadline 5
```

This sends one GET to `/readyz` at a literal loopback address. It prints the
validated health JSON and exits `0` if a receive-capable session is present,
or `3` if the relay correctly reports no ready session. Exit `1` means no
trustworthy readiness observation (for example a timeout, incompatible schema,
or HTTP status inconsistent with the peer counts), and produces no JSON.
When supplied, `--expect-network mainnet|testnet` also returns `1` without
printing JSON if the relay reports a different network, even if it is ready.
Without this option, either internally consistent network/chain ID pair is
accepted. This checks reported configuration only, not chain identity on the
wire. Invalid arguments exit `2`. The optional POSIX deadline works like the
collector's; without it, the two-second socket idle timeout remains in effect.
No proxy, redirect, submission, peer dial, or external RPC is requested by
this tool. As with any `/readyz` request, Relay may prune already-expired cache
entries while producing the health response. It does not establish delivery
or chain health.
The delivery-binary smoke test runs this exact CLI on isolated IPv4 and, when
available, IPv6 listeners. With no peers it expects exit `3` and JSON matching
`/healthz`. Ready sessions and transitions are covered by separate fixtures.

`BenchmarkHealthObservation` measures direct serial/parallel `Health()` reads
and the readiness handler plus `ResponseRecorder`, with 0/20/200 synthetic
sessions and 0/1024 cached entries. Run it with the qualified Linux linker policy:

```sh
go test -ldflags=-extldflags=-Wl,-z,noexecstack ./internal/relay \
  -run '^$' -bench '^BenchmarkHealthObservation$' -benchmem -count=3
```

Unlike `Stats()`, `Health()` takes peer/cache locks and prunes expired cache
entries. Empty-cache health reads skip the clock and expiry check while still
taking the same cache lock. Nonempty reads retain the expiry check. The
benchmark keeps entries unexpired, so it does not measure expiry
cohorts, live session churn, socket latency, or transaction throughput. Compare
results only with matching toolchain/CPU/concurrency; timings are not CI gates.

Chain ID display text is computed when constructing the immutable chain view,
not during each health poll. `TestHealthChainIdentityAndAllocations` checks the
mainnet/testnet identity and zero allocations for direct steady-state `Health()`;
a separate regression checks replacement synthetic chain views. This does not
make `Health()` lock-free or make HTTP JSON encoding allocation-free.
