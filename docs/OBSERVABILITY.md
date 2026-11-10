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
