# Experimental operator runbook

Status: **NOT PRODUCTION READY**. This is a pre-deployment rehearsal and incident
guide for the Stage 1, memory-only signed-transaction relay. It does not authorize
deployment, public-network testing, or real transaction submission. See the
[production gates](PRODUCTION_READINESS.md) for requirements still open.

The pre-statistics commands and contracts below were checked against local source
`c4e10ac253b9ba9e014091e169453ce11480b778`, whose tracked content matches published
commit [74b47eb3cebaa2a4a2461926dfa6410e4b23b78b](https://github.com/keienWang/bsc-p2p-relay/commit/74b47eb3cebaa2a4a2461926dfa6410e4b23b78b)
(tree `513b9edf6531754714d815057e88676d7e6f71c6`). This source reference is not a
hosted-CI or release-readiness claim. Resolve the eventual release commit and its
own evidence before use. Runtime sources are
[CLI](../cmd/bsc-relay/main.go), [HTTP](../internal/relay/http.go),
[node/queues](../internal/relay/node.go), [cache/validation](../internal/relay/store.go)
and [network selection](../internal/relay/network.go). The separately versioned
statistics endpoint is newer than that operator-only source reference; see its
[current contract](OBSERVABILITY.md) and final-source verification evidence before
use. This guide does not certify that a new binary has passed its release gates.

## Before starting

- Assign an operator and a stop/rollback decision maker. Agree the intended
  network, permitted peers, transaction source, traffic budget and recovery
  behavior. Alert durations, thresholds and SLOs require business requirements
  and measurements on target hardware; none are certified here.
- Record the exact source commit, binary checksum, Go version, dependency review
  and configuration. Check [verification records](VERIFICATION.md) against that
  source, not a nearby commit or an older CI badge. Use the supported patched
  toolchain in [README](../README.md); this guide does not rerun release gates.
- Keep the HTTP listener on a literal loopback IP. Do not expose it with a public
  bind, reverse proxy, tunnel, container port publication or port forward. There
  is no API authentication or remote administrative endpoint. Host/Origin
  checks do not isolate other users or processes on the same machine.
- Do not load or retain wallet private keys, seed phrases, wallet passwords or
  signing authority. Sign elsewhere. The generated ephemeral P2P identity is
  not a wallet key. This relay has no RPC endpoint/client, historical chain
  database, transaction-status lookup or persistent delivery queue.
- The relay binary still includes and initializes upstream consensus-cryptography
  dependencies, including Prysm/BLST/Herumi. The separate standard-library-only
  research supervisor does not isolate the relay. Review the unresolved
  [BLS concurrency/resource gate](BLS_CACHE_KNOWN_ISSUE.md); this is neither a
  claim of a proven remote exploit nor a completed production safety gate.
- Treat raw signed transactions as broadcastable material. Do not put them in
  command history, shared diagnostics or public issues. Once sent to a peer,
  they cannot be recalled by clearing this process. Test only with approved,
  nonvaluable fixtures and isolated peers unless a separate network/transaction
  authorization exists.
- Distinguish the two network surfaces: HTTP is loopback-only, but the default
  P2P listener is all interfaces and mainnet discovery is enabled. Use the
  explicit isolated command below for local rehearsal. `--no-discovery` alone
  is not isolation if a public static peer or public P2P bind remains configured.

## Implemented configuration

Only the following application flags are registered. `--help` displays Go's
generated flag help. Limits are startup configuration, not hot-reload settings.

There are no subcommands or positional arguments. Any leftover argument causes
startup to fail before a Node is created or either listener opens, with
`unexpected positional arguments; use --help for supported flags`. This also
rejects arguments after `--`; a bare final `--` is allowed. Go flag parsing stops
at the first positional argument, so allowing leftovers could silently ignore
later flags such as `--network testnet`. For explicit boolean values use
`--no-discovery=false` or `--no-discovery=true`, not `--no-discovery false`.
The positional-error message deliberately does not print supplied arguments.

<!-- operations-flags:start -->
| Flag | Default | Validation or consequence |
| --- | --- | --- |
| `--network` | `mainnet` | Exactly `mainnet` (chain ID 56) or `testnet` (97). |
| `--p2p` | `:30311` | P2P listen address; use `127.0.0.1:0` for an isolated, ephemeral TCP port. Binding errors fail startup. |
| `--api` | `127.0.0.1:8787` | Literal loopback IP and port only, for example `127.0.0.1:8787` or `[::1]:8787`. `localhost:8787`, `:8787` and `0.0.0.0:8787` are rejected. |
| `--no-discovery` | `false` | Disables discovery, not static peer dialing or the P2P listener. |
| `--max-peers` | `20` | Integer from 1 through 200. This is not a required-ready-peer count. |
| `--cache-count` | `1024` | Integer at least 1. No additional application maximum is validated. |
| `--cache-bytes` | `16777216` | Integer at least 131072. Counts serialized transaction bytes, not process RSS. |
| `--cache-ttl` | `2m0s` | Positive Go duration, for example `2m`. No additional application maximum is validated. |
| `--peer` | none | Repeat for static enode URLs. Invalid URLs fail startup. |
<!-- operations-flags:end -->

Mainnet uses official BSC mainnet bootnodes only when discovery is enabled.
Testnet requires at least one explicit `--peer`, even with discovery disabled;
it never substitutes mainnet bootnodes. Increasing peer/cache limits is not a
capacity fix until the resource and retention budget has been reviewed. There
are no flags for an RPC URL, wallet key, datadir, persistent node key, queue size,
metrics exporter, authentication or log level.

## Local start, inspection and stop

From the repository root, with an already built, reviewed binary at
`./bin/bsc-relay`, start in the foreground:

```sh
./bin/bsc-relay --network mainnet --no-discovery \
  --p2p 127.0.0.1:0 --api 127.0.0.1:8787
```

This selects mainnet encoding/genesis locally but supplies no peers and does not
enable discovery. It is not a mainnet broadcast exercise. After validating the
configuration, the CLI reserves the HTTP API listener before starting P2P. An API
bind failure therefore exits without starting P2P or printing a successful
startup message. If P2P startup fails, the reserved API listener is closed.
The binary prints a startup message with network, API address and ephemeral
enode only after both bind/start steps have succeeded. This is not an endpoint
health check or a guarantee against later server errors: verify process survival
and the endpoints.

In another terminal, bypass any configured HTTP proxy and do not follow redirects:

```sh
curl --noproxy '*' --connect-timeout 2 --max-time 5 -i http://127.0.0.1:8787/healthz
curl --noproxy '*' --connect-timeout 2 --max-time 5 -i http://127.0.0.1:8787/readyz
```

Expected in this isolated mode: `/healthz` is 200, `ready_peers` and
`connected_peers` are 0, `confirmation_tracking` is false, and `/readyz` is 503.
That 503 is expected, not a reason to enable public discovery during a smoke test.
For an automatic local start/check/stop, the existing
[smoke script](../scripts/smoke.py) chooses a temporary loopback API port, disables
discovery, supplies no peer, sends only malformed transaction bytes and checks
shutdown plus the absence of data files:

```sh
python3 scripts/smoke.py ./bin/bsc-relay
```

Before that local startup, the smoke checks six positional-argument rejection
cases against the same binary. These cases put an invalid API address before
any leftover argument, so even the older CLI without this check exits before
creating a Node. The command-package Go tests retain this guard and separately
check that flag-only arguments, including a bare final `--`, reach the normal
address-validation path. These are argument-parsing tests, not delivery tests.
The smoke also holds two test-owned loopback ports and checks that an API bind
conflict is reported before the P2P conflict, without a startup-success message.
Neither the old nor the corrected ordering can start P2P with this fixture.
The Go command-package tests additionally check that a later P2P startup failure
releases the reserved API listener. No existing service is used as a fixture.

Stop caller submissions first. Send Ctrl-C to the foreground process, or SIGTERM
to the verified relay PID. The CLI gives HTTP shutdown a five-second context and
then closes the P2P server. This is **not a delivery drain**: queued transactions
can be abandoned. Wait for process exit and record its status. Do not use a
forced kill as an ordinary shutdown or treat a clean exit as a delivery receipt.
Any uncompleted or unanswered client request has an uncertain outcome.

### Node lifecycle callers

Repository code embedding `internal/relay.Node` must create a fresh Node after
Close or a failed Start. Calling Start on the old instance returns
`node is closed; create a new node to restart` (`ErrNodeClosed`). This is a
lifecycle-method error, **not a Submit error or an HTTP rejection response**.
The CLI has no in-process restart path. See the [single-use lifecycle contract,
cleanup review and test limits](NODE_LIFECYCLE.md).

## HTTP contract and client decisions

`POST /v1/transactions` takes `Content-Type: application/json`. The media type is
parsed case-insensitively with Go `mime.ParseMediaType`; valid whitespace and
parameters are accepted. Malformed parameters or conflicting duplicate parameter
values receive 415. The body must be one JSON object containing a `raw_tx` string, such as
`{"raw_tx":"0x..."}`. The ellipsis is illustrative, not a valid transaction.
Send exactly one `raw_tx` key. Unknown fields and trailing JSON values are
rejected. The server uses Go `encoding/json` with `DisallowUnknownFields`; it is
not a separate strict duplicate-key/case-sensitive JSON validator.

Raw bytes must be 1 through 131072 bytes, encoded as `0x`-prefixed hex. The body
reader limit is 263168 bytes (`2 * 131072 + 1024`); exceeding it is a JSON/body
400 path, not an implemented 413 response. Eight concurrent transaction-request
slots bound handler admission. The CLI sets header/read/write/idle timeouts to
3s/5s/5s/30s and `MaxHeaderBytes` to 8192. Client deadlines and retry limits still
need to be explicit; a network timeout is not a negative acknowledgment.

Local validation requires canonical, replay-protected types 0, 1 or 2 for the
selected chain, a recoverable signature, gas at least 21000, uint256 value/fee
bounds and priority fee no greater than max fee. It does not check current nonce,
balance, full intrinsic gas, current fork applicability, execution or inclusion.

### Successful response: 202

<!-- operations-submission:start -->
| Field | Meaning |
| --- | --- |
| `hash` | Locally computed transaction hash, 32-byte hex. |
| `sender` | Locally recovered signer address, 20-byte hex. |
| `queued_peers` | Number of new local peer-queue admissions in this request, not acknowledgments. |
| `duplicate` | The hash was already live in the local cache at admission. |
| `stage` | `queued` if at least one queue admission occurred; `cached` for a duplicate with no new eligible peer to cover. |
<!-- operations-submission:end -->

One successful queue admission is sufficient for 202, including partial fanout.
Other peers can be slow, full or unavailable. A duplicate with valid prior sent
records for all considered peers can return 202, `queued_peers: 0`,
`stage: cached`. This says nothing about whether the prior writes succeeded.

### Rejection responses

Handler errors have JSON shape `{"error":"message"}`. Health/readiness use the
health object instead, including readiness 503. HTTP server/parser errors or a
broken connection need not produce this application JSON schema.

| Status | Exact application error or category | Operator/client action |
| --- | --- | --- |
| 400 | `invalid JSON request`; `exactly one JSON object is required`; `raw_tx must be 0x-prefixed hex`; `invalid transaction hex` | Fix the request. Do not repeatedly retry identical malformed input. |
| 400 | Validation rejection, including `raw transaction size must be 1..131072 bytes`, unsupported type, wrong chain/replay protection, gas/value/fee constraints, noncanonical encoding, or `decode signed transaction: ...` / `invalid signature: ...` | Inspect the error and signing/request construction outside this relay. Do not change transaction financial terms automatically. |
| 503 | `no compatible transaction-receiving peer is ready` | No new cache insertion occurred in this request. Check peer readiness, then use bounded retries only while the transaction is still authorized for broadcast. |
| 429 | `too many concurrent requests` | All eight request slots were occupied; this request did not parse or insert a transaction. Reduce caller concurrency. |
| 429 | `transaction memory cache is full` | New hash cannot fit count or serialized-byte capacity. Both limits share this error. No new entry was inserted. Inspect occupancy and arrival/retention budgets. |
| 429 | `all peer queues are full; retry later` | No new queue admission succeeded. A fresh cache insertion is rolled back; a previously cached hash remains. A closing session or bounded peer sent-map can also make admission unavailable: this string is not a queue-depth measurement. |
| 429 | `transaction expired during admission; retry later` | Its cache deadline passed during admission. Expired state is pruned; do not count it as a successful live admission. |
| 403 | `invalid local Host header`; `browser-origin requests are not allowed` | Use the direct local endpoint with a loopback/localhost Host and no nonempty Origin. Do not bypass this with a public proxy. |
| 405 | `GET required`; `POST required` | Use the documented method for the selected path. |
| 415 | `application/json required` | Set the JSON content type. |
| 404 | `not found` | Check the path; there is no transaction lookup, RPC, `/metrics` or profiling endpoint. |

Validation occurs before checking peers, so an invalid transaction returns 400
even when there are no ready peers. Readiness is a snapshot: a peer can disconnect
between `/readyz` and submit. An already cached transaction can receive 503 or
429 on a later attempt without retracting an earlier queue admission.

### Bounded retries, duplicates and TTL

- Keep a bounded caller record of transaction hash, attempts, timestamps and
  observed responses. Set a maximum attempt count, elapsed retry deadline and
  concurrency budget before sending. Do not create an unbounded retry queue.
- For retryable 429/503 or uncertain transport outcomes, an example policy is
  jittered exponential delay capped at a caller-chosen maximum. Both maximum
  attempts and elapsed time must stop the loop. Select those values from the
  approved transaction lifetime and load budget, not an invented relay SLO.
  There is no application `Retry-After` header or server-side retry scheduler.
- Retry the **same signed bytes** only if continued broadcasting is still
  authorized. A lost response can follow successful queue admission. Resigning,
  changing fees/nonces, switching network, or generating replacements is not a
  transport retry and is outside this runbook.
- A cache lifetime starts at first insertion plus configured TTL. Duplicates,
  queue admissions and successful writes do not extend it or free capacity.
  The same peer session is not enqueued again during that lifetime. A new or
  previously uncovered eligible session can be covered by another submission.
- Connecting a peer does not automatically replay the cache. A permitted caller
  resubmission can trigger new fanout; peers may separately request live cached
  transactions. No durable outbox, remote acknowledgment or automatic delivery
  reconciliation exists.
- After expiry or restart, the same bytes can be admitted with `duplicate: false`
  again. That is a new local cache lifetime, not evidence that a previous attempt
  failed on-chain. No HTTP response gives the exact cache expiry timestamp.
- Writers skip queued transaction messages whose deadline has passed **before
  write start**. A write already started can finish after TTL under upstream
  transport timeouts. Disconnect/exit can abandon queue contents. A 202 must
  never become a payment-success or guaranteed-delivery decision.

## No-peer and transport failure triage

1. Check process exit status and startup errors first. An HTTP bind conflict,
   invalid literal API address, invalid network/cache limit or malformed static
   peer is a configuration/startup failure, not evidence of a chain outage.
2. Check `network` and `chain_id` in health against the signed transaction source.
   In the isolated command above, zero peers is intentional. For an independently
   approved network experiment, inspect the existing reviewed discovery/static
   peer settings; do not substitute random peers or another network to make
   readiness green.
3. `connected_peers` counts sessions in the relay's map **after both handshakes**,
   not all TCP sockets or dial attempts. Zero can mean reachability, incompatible
   eth/68/network/genesis/fork IDs, handshake timeouts or churn. There is no API
   listing individual peers or handshake-failure reasons. Use available process
   diagnostics and authorized peer-side diagnostics; do not claim the aggregate
   counters identify a cause they cannot expose.
4. Positive `connected_peers` with zero `ready_peers` can mean all sessions opted
   out of transaction broadcast through UpgradeStatus. A handshake alone is not
   willingness to receive transactions. Review the permitted peer configuration
   with its owner, rather than treating the peer as faulty or trustworthy.
5. A rising `write_failures` counter means a writer send failed and that session
   is disconnected. It does not reveal which transaction was accepted elsewhere.
   Stop aggressive retries, inspect caller status/error distributions and resource
   use, then resolve reachability or slow-peer issues within the approved scope.
6. If no authorized peer is usable, pause new offers or let the caller's bounded
   retry policy terminate. Report unavailable transport. Do not add external RPC,
   expose HTTP, disable validation or promise delayed automatic delivery.

## Observation and capacity

`GET /healthz` returns 200 if its guarded handler completes; it is not a general
resource or chain-health diagnosis. `GET /readyz` returns the same object with
503 when `ready_peers == 0`, otherwise 200. Both require GET, valid local Host and
no nonempty Origin. The object has exactly these current fields:

<!-- operations-health:start -->
| Field | Meaning and limits |
| --- | --- |
| `network` | Selected `mainnet` or `testnet`. |
| `chain_id` | String `"56"` or `"97"`, not a JSON number. |
| `ready_peers` | Handshaken sessions that advertise receiving transactions. |
| `connected_peers` | Sessions registered after the dual handshake, including opt-out sessions. |
| `cached_transactions` | Live cached hash count after a prune at observation time. |
| `cached_bytes` | Serialized size total for those live entries; not RSS. |
| `written_messages` | Successful outbound writer messages since process start, including protocol replies, not only transactions. Not peer acceptance or chain confirmation. |
| `write_failures` | Failed writer sends since process start. Not a count of every handshake/disconnect/error. |
| `confirmation_tracking` | Always `false` in this milestone. |
<!-- operations-health:end -->

Counters reset on restart. Peer and cache samples are not one global atomic
snapshot; ordinary churn can change them between observations. Collect bounded,
timestamped samples and caller-side status/error/latency counts, with failures
and uncertain outcomes retained. Do not infer exact per-transaction history from
aggregate counter differences.

`GET /v1/stats` separately exposes eight normally completed Submit outcomes and
HTTP's pre-Submit concurrency-rejection count. It preserves the health object
above and is 200 even without peers. Use the same guarded local listener:

```sh
curl --noproxy '*' --connect-timeout 2 --max-time 5 -i http://127.0.0.1:8787/v1/stats
```

See the [frozen schema and collection contract](OBSERVABILITY.md). Rejected cache,
queue-unavailable and admission-expired outcomes are distinct from the HTTP
concurrency refusal; count/byte cache failures remain combined. New cache
lifetimes are not globally distinct hash offers. Other early HTTP failures do
not call Submit, so these counters do not provide a complete HTTP denominator.
Use same-endpoint, same-instance serial scrapes and caller monotonic time for
approximate rates. Treat failed/invalid/unsupported observations as unavailable,
not zero or newly refreshed stale values. A 404 from an older binary is absence
of this endpoint, not evidence of zero traffic. No SLO is certified here.

For a separately verified relay PID, Linux/Unix tools can inspect actual process
resources without adding a remote endpoint. For example, after assigning `PID`
to that process, use `ps -p "$PID" -o pid,etime,time,pcpu,rss,args` and, on Linux,
read `VmRSS`/`VmHWM` in `/proc/$PID/status`. Check platform units: procps `rss`
and Linux status `kB` are KiB; `ps` `%CPU` is a lifetime average, not an interval
peak. For interval CPU, sample process CPU time or `/proc/$PID/stat` deltas with
the documented clock-tick rate and elapsed time. Verify PID identity after
restarts and state whether results are process-wide or container-wide.

The HTTP API exposes **no CPU, RSS, heap, goroutine count, queue depth, queue wait,
per-peer sent-map count, expiration/drop count or latency histogram**. The
64-item outbound queue is per peer, shared with protocol responses, with up to
one additional in-flight item. `queued_peers` is a per-request admission count,
not queue occupancy. Exact internal queue samples exist only in the local
[soak](SOAK.md) and [unique-capacity](UNIQUE_CAPACITY.md) test harnesses, which
share their process with clients, fixtures and observers. Their heap/RSS are not
isolated relay measurements; cache-age-to-write is not exact queue residence.
Do not run a large soak automatically during an incident or invent missing
production metrics. Reviewed instrumentation is a remaining operational need.

For retention `T` seconds, admitted new-hash rate `lambda` and extra burst `B`,
plan at least `ceil(lambda * T) + B` entries, and enough serialized-byte capacity
for the maximum rolling retention-window byte sum. Both limits apply. Average
transaction size alone is not a safe bound for heavy-tailed/adversarial input.
Default count and TTL imply an ideal long-run upper bound of
`1024 / 120 = 8.533...` new cache lifetimes/s before byte, CPU or peer constraints.
Repeated duplicate HTTP requests are not new-hash throughput. A short test across
one expiry boundary can exceed that average without proving steady-state capacity.

Measure after retention warmup over multiple retention windows, with actual size
mix, bursts, peers and slow-writer conditions. Sampling can miss short queue,
CPU or RSS peaks; disclose cadence, duration, platform and failures. Choose alert
thresholds from required new-hash rate, retry/retention/fanout policy, memory
budget and latency/recovery goals. There are no certified alert thresholds here.

Cache bytes exclude decoded objects, map/heap allocation, GC headroom, HTTP and
transport buffers, goroutines and **O(peer count * cache count)** sent metadata.
Queues/in-flight messages can retain expired objects beyond cache TTL. Cache
expiry releases store references, not necessarily memory to the OS. Do not
interpret zero cache occupancy or an expiry benchmark as zero RSS or leak freedom.

## Upgrade, rollback and bounded-state cleanup

1. Before a change, retain the reviewed old binary/checksum, source/configuration,
   caller policy and bounded diagnostics. Verify the new exact commit's tests,
   dependency/security evidence, hosted CI and distribution notices as applicable.
   Historical measurements and the source-pinned SBOM describe their stated
   revisions, not automatically a new candidate. Review BSC/fork compatibility.
2. Pause new offers and decide explicitly how the caller records uncertain and
   previously accepted submissions. Do not rely on draining from health: there
   is no queue-depth endpoint or delivery drain command. Record a final health
   sample and stop normally. Confirm exit before reusing the listener addresses.
3. Rehearse the replacement with discovery disabled, no static peer and loopback
   listeners. Validate health, expected no-peer readiness, malformed-input
   rejection and clean shutdown using the existing smoke script. Only a separately
   authorized rollout may then restore the reviewed network/peer configuration.
4. On regression, stop the new process, restore the compatible previous binary
   and configuration, and repeat the local checks before an authorized restart.
   An older binary can be incompatible with a newly activated network fork;
   rollback is not permission to ignore security or protocol gates. Do not run
   both versions against the same addresses or silently duplicate caller traffic.
5. Restart discards all cache entries, peer sent metadata, queues, transport
   counters and the ephemeral node identity. There is no chain database to
   migrate/restore, persistent transaction replay log or cache-clear API. The
   one-second maintenance tick prunes expired cache/sent state while idle;
   accesses also enforce expiry. If an immediate local state reset is necessary,
   a deliberate stop/start does it at the cost of abandoning transport state.
6. Pruning clears references but retains bounded reusable allocation; GC/RSS
   release is not immediate or guaranteed by the TTL. Process exit releases its
   memory, not any signed transaction already obtained by a peer. Do not delete
   unrelated files or external signer records as a relay cleanup procedure.
   Reconcile caller records before any permitted resubmission. Never equate
   `duplicate: false` after restart with an on-chain failure.

For incident records, retain the source/binary/configuration identity, UTC times,
process exit/error information, bounded health and OS-resource samples, and
caller counts by outcome. Exclude keys and raw valuable signed transactions;
share only necessary, reviewed details through an appropriate channel. Follow
[security reporting guidance](SECURITY.md#reporting-issues).

## Lightweight documentation check

From the repository root:

```sh
python3 scripts/check_operations_docs.py
```

This standard-library-only check reads local files. It compares flag names and
JSON field names with source declarations, checks literal handler/admission error
strings, and checks relative file/heading links in this runbook plus its
README/readiness/security entry points. It is a narrow text check, not a Go or
Markdown parser. Its supported forms are the current direct flag registrations,
explicit JSON tags, direct handler calls and single-variable literal admission
errors. Regression tests reject known unsupported shapes including aliases,
receiver registrations, commented selectors, concatenated names, grouped error
declarations, untagged fields and duplicate names. These rejections are not an
exhaustive guarantee for arbitrary Go syntax: changes to declaration style,
aliases, error construction or structure still require manual review even if a
text check passes. Literal handler errors support numeric or `http.Status...`
status constants; the existing `status, err.Error()` forwarding call is recognized
separately, not interpreted as a complete dynamic error list. The checker does
not verify default values, ranges, field types, complete JSON semantics or dynamic
decode/signature errors. It neither starts a node nor
accesses the network and does not validate runtime behavior, external URLs,
timing, capacity, CI or production readiness. Use the separate release
verification gates for those claims.
