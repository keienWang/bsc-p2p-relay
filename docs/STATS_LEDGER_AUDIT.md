# Independent submission-statistics audit

This is a short test-only accounting check of the frozen
[`/v1/stats` contract](OBSERVABILITY.md), not a collector, load measurement,
production-readiness result or remote acceptance/confirmation claim.

## Independent evidence

`stats_ledger*_test.go` uses a separate literal field table and uint64 decoder.
It does not read `Node.stats`, call `Node.Stats`, or use production outcome enums
or statistics DTOs as expected values. Real Handler or loopback HTTP responses
provide actual observations. Strict object parsing rejects duplicate, unknown,
missing and null fields; raw integer decoding retains precision above 2^53 up to
MaxUint64. Status, headers, metadata and instance identity are checked.

Short scenarios reuse the validated HTTP/per-hash ledger from
`unique_capacity_test.go`, including its fail-closed checks, unchanged. Expected
partitions are declared before requests and checked against that ledger before
comparing statistics. Cache deadlines and final-state diagnostics in the old
harness are not inputs to the statistics oracle. A short TTL wait starts after
request completion and uses the known configuration, not a cache observation.

Coverage includes count and byte pressure, live cached retry, the same hash after
TTL, fresh rollback followed by retry, full queues, partial fanout, cached retry
of an uncovered peer, peer replacement/no peers, closing sessions and sent-map
bounds. A four-caller start barrier and join cover concurrent unique admission.
All eight Submit fields are checked after callers complete; `other_total` remains
zero. Admission-expired coverage explicitly injects a negative **internal** TTL
that `New` rejects. It does not demonstrate that normal configuration reaches
that branch.

The two Handler fixture acquires eight slots in each Handler, proves independent
limiters, verifies two normal and one failed-write concurrency-refusal decisions,
then releases and joins all sixteen requests. Both Handlers and a fresh Handler
observe shared Node counters and identity. Parse/hex and Host/Origin/media/method
refusals are separately excluded from Submit. Controlled error responses must
match the predeclared exact reason as well as their status and JSON schema. No
equality assertion relies on an in-flight, multi-atomic snapshot.

## Failure evidence and denominators

A controlled ResponseWriter barrier separates Submit completion from response
write success. A failed response does not erase a known acceptance. The identical
injected client transport error is also tested both before dispatch and after a
completed Handler: client-only accounting is incomparable in both cases, while
fixture-known execution distinguishes zero from one normal Submit return.
Unknown client outcomes are never converted into a Submit rejection or a zero.
The existing unique harness still fails on client/read/close/invalid responses;
its old HTTP-500 failure behavior and report schema are not relaxed.

The Submit denominator is known normal returns, not offered distinct hashes,
old-hash retry attempts, local queue items, or all HTTP traffic. A retry after
expiry/rollback may be an accepted new cache lifetime. Partial fanout counts one
Submit. HTTP concurrency refusals have a separate counter and are excluded from
the Submit sum. Unrecognized or contradictory response-ledger partitions fail.

Mutation tests require the auditor to reject per-field under/overcounts, all 36
pair swaps across nine distinct synthetic counter values, wrong denominators,
unknown execution, changed identity/creation metadata, decreasing counters and
sum overflow. Nonzero synthetic `other_total` is only parser/auditor test data.
Semantic response mutants must first be valid JSON; key field/type/missing
mutants must fail for their intended reason, not an unrelated parse failure.
Bad scrape tests include non-200 responses, malformed/trailing/unknown-schema JSON,
all counter type/precision failures, read/close faults, oversized bodies, and a
real HTTP response whose Content-Length exceeds its complete JSON prefix.
Failures return an error and no usable sample; no previous sample is substituted.

## Running the bounded check

With the repository's supported Go toolchain and cached dependencies:

```sh
go test -race -count=1 -timeout=90s -run '^TestStatsLedger' ./internal/relay
```

The cases are local-only: existing in-process MsgPipe/official handshake fixtures,
loopback HTTP, deterministic public test keys and synthetic zero-value data. No
public P2P/discovery, RPC, real account or real payment is used. No default-TTL
150-second run, soak or benchmark is required for this audit. Full release checks
remain those in `CONTRIBUTING.md`; a focused pass is not an aggregate pass.

The exact tested source, command results and evidence hashes are recorded in
[STATS_LEDGER_VALIDATION.md](STATS_LEDGER_VALIDATION.md).
