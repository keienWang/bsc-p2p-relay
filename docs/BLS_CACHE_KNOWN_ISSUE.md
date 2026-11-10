# Known upstream BLS cache concurrency boundary

Status: **unresolved production blocker for future concurrent BLS verification**.
The relay does not enable modern header verification or a checkpoint importer.
The sequential, synthetic checkpoint proof is not evidence of concurrency safety.

## Observed result

With the pinned `github.com/prysmaticlabs/prysm/v5 v5.3.2`, concurrent first parses
of the same valid BLS public key reproduce a Go data race. The diagnostic exits
1 with `WARNING: DATA RACE` and `race detected during execution of test`.
The competing paths are `cache/nonblocking/lru.go` `Add` (entry-value write,
line 55) and `Get` (entry-value read, line 88), reached through
`crypto/bls/blst.PublicKeyFromBytes`. A cache-value/public-key copy race also
appears in the captured trace. The input uses deterministic public test keys.

This establishes a concurrent data race. Remote exploitability, incorrect
signature acceptance and a cryptographic verification bypass were not shown.
A static `govulncheck -test` scan reports no vulnerabilities for this package;
that scan does not detect or negate the observed runtime race.

## Reproduce explicitly

From the repository root with the project's Go toolchain:

```
go test -race -tags=blscacherace -count=1 -timeout=60s \
  -run '^TestBLSPublicKeyConcurrentFirstUse$' ./internal/checkpointproof
```

`known_bls_cache_race_test.go` is an opt-in diagnostic for a known unsupported
concurrent scenario. It is intentionally not represented by the default green
sequential-proof job, and its failure must not be described as passing. Race
reports depend on scheduling; one passing attempt is not proof of a fix.
The reproduction does not use network access, RPC, transactions or user keys.

## Resource scope

The BLS cache is process-global, not owned by `Parlia.New`/`Close`. In this pin it
holds up to 2,000,000 public keys, creates a 200,000-entry channel and starts a
permanent goroutine. Recreating an engine does not reset those resources. The
cache's entry limit is not a measured process RSS bound. The engine's snapshot
and signature-cache limits therefore do not establish a complete memory budget.

Before implementing an importer or enabling live/concurrent BLS validation:

- Review a maintained upstream remediation or prove a process-level isolation
  design with bounded work, memory, lifetime and concurrency.
- Include all library callers and the cache goroutine. A mutex owned by one
  verifier instance does not protect a global cache shared by other instances.
- Authenticate the complete trusted bundle and bound its bytes/counts before
  parsing public keys. A digest acquired from the same untrusted peer is not trust.
- Test cold and warm keys, same and distinct keys, malformed/subgroup-invalid
  keys, aggregate signatures, resource exhaustion, restarts and concurrent work.
- Preserve all cryptographic validation and the race detector. Do not turn off
  verification, suppress the detector or treat an unrelated green scan as a fix.

This research batch changes no upstream BLS/cache implementation. Any remedy
requires a separate reviewed commit and independent dynamic verification.

## Pinned source

The Go module origin identifies v5.3.2 as commit
`21e1f7883b88ce39d16263d1cdea70bfdaa4d359`:

- [Public-key cache and parsing](https://github.com/prysmaticlabs/prysm/blob/21e1f7883b88ce39d16263d1cdea70bfdaa4d359/crypto/bls/blst/public_key.go)
- [Global cache initialization](https://github.com/prysmaticlabs/prysm/blob/21e1f7883b88ce39d16263d1cdea70bfdaa4d359/crypto/bls/blst/init.go)
- [Asynchronous LRU implementation](https://github.com/prysmaticlabs/prysm/blob/21e1f7883b88ce39d16263d1cdea70bfdaa4d359/cache/nonblocking/lru.go)
