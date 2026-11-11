# Single-use Node lifecycle

Status: local lifecycle hardening, **not production readiness**. This change does
not alter wire messages, transaction admission, HTTP or statistics schemas.

## Defect and supported behavior

The previous `Node.Close` used a once-only guard without coordinating with
`Node.Start`. Calling Close before Start consumed that guard and closed the
maintenance/writer stop signal. A later Start still opened a P2P listener and
returned success. Maintenance and future writers then exited, and another Close
could not stop the new server. Concurrent Start/Close could reach the same order.

This is a local API lifecycle defect, not a demonstrated attack by a remote peer.
`internal/relay.Node` exports these methods to the repository's callers; it is not
an externally importable public Go package. The current CLI calls New, Start,
then defers Close. It has no stop/restart path and does not execute the reproduced
Close-before-Start sequence.

The supported contract is now:

- A Node is single-use. Start succeeds at most once.
- Close is terminal and idempotent, even if Start has never been called.
- Start after Close returns `ErrNodeClosed`; create a fresh Node to restart.
- A failed Start is also terminal. Its original startup error is returned, and a
  later Start returns `ErrNodeClosed`. Retrying configuration requires a new Node.
- A duplicate Start on a running instance returns an error without stopping that
  instance or replacing its maintenance worker.
- Start and Close serialize on a separate lifecycle mutex. Close waits for this
  node's maintenance goroutine before returning. Concurrent Close callers wait
  for the same serialized shutdown.
- Transport shutdown still uses the pinned upstream server. Its Stop can return
  on its own 15-second timeout; this patch does **not** promise that every peer
  goroutine or network write has stopped at that instant.

No peer or cache lock is held while acquiring the lifecycle mutex. The maintenance
loop takes cache, submission and peer locks but never calls Start/Close or acquires
the lifecycle mutex. Protocol handlers likewise do not acquire it. The review
found no new lock-order cycle from waiting for maintenance under that mutex.

## Pinned upstream ownership review

All source references use BSC v1.8.2, commit
`54afaff4da31ac9e49ba646a4f65104edac5a2f3`:

- [Server.Start/Stop and initialization](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/p2p/server.go):
  Start explicitly documents that a stopped server cannot be reused. It sets
  running before setup, initializes quit, opens the node database, starts port
  mapping, opens TCP, configures discovery, then starts the dial scheduler and
  server run loop. Stop closes the listener and quit and waits for workers, with
  the upstream timeout. The successful run loop owns database cleanup.
- [Node database](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/p2p/enode/nodedb.go):
  the empty database path creates an in-memory LevelDB, not a persistent chain
  database. Its worker resources still need Close.
- [LocalNode.Database](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/p2p/enode/localnode.go)
  exposes the initialized node database. On a failed Start, the relay calls Stop
  and then closes this database, if initialized. It does not manually close the
  successful run loop's database or race that ownership.
- [Port mapping startup](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/p2p/server_nat.go):
  even NAT=nil starts a worker, which exits on quit. A TCP bind error occurs after
  this worker and the database have been initialized but before the run loop
  starts. Stop alone does not close that failed-start database.

The relay's New always supplies a generated non-nil P2P identity key, an empty
node-database path, NAT=nil and DiscoveryV5=false. The upstream nil-key failure
before quit initialization is therefore not reachable through this configuration.
Database-open failure occurs after quit initialization and before LocalNode is
set, so the cleanup's nil check is necessary.

For this pinned configuration, a discovery-v4 construction error occurs before
its worker goroutines start; after successful v4 construction there is no later
error-returning startup step. The inbound SetupConn path, including RLPx,
nodeFromConn and the server checkpoints, does not access the node database.
Closing a failed-start database therefore does not introduce a discovered
shared-database race with those pending connections. This review is not a generic
cleanup guarantee for arbitrary future upstream server configurations.

## Unverified upstream startup boundary

A separate source-level concern remains, and was **not reproduced or fixed** in
this work. It must not be presented as a verified remote exploit:

1. server.go starts TCP listening before setupDiscovery. A UDP bind failure can
   return before setupDialScheduler has assigned `srv.dialsched`.
2. An accepted inbound SetupConn can contend for `srv.lock` after Start returns.
   If it sees running=true before Stop changes it, it can proceed into RLPx.
3. Successful encryption establishes `c.node`. The following checkpoint may
   return when quit is closed.
4. SetupConn's defer (server.go lines 935-938 in the pinned module) calls
   `srv.dialsched.inboundCompleted` for an inbound connection with non-nil c.node.
   [dial.go](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/p2p/dial.go)
   accesses scheduler fields without a nil guard.

The current local tests use discovery disabled and no incoming peers during
failed startup. They do not cover this TCP/UDP failure interleaving, failure after
UDP-socket creation, arbitrary modified bootstrap configurations, public-network
startup or adverse shutdown traffic. A separately authorized deterministic
experiment and an upstream-compatible remediation review are still needed.

## Peer I/O review limits

The initial remote-input review did not establish an unbounded peer goroutine
leak in the relay. In the pinned
[peer implementation](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/p2p/peer.go),
protocol exit closes the peer, and protoRW's closed channel unblocks reads and
writes waiting to start. The pinned
[RLPx transport](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/p2p/transport.go)
applies the server's 20-second frame write timeout and 30-second frame read
timeout. The relay's handshake exchanges have five-second timeouts and its
outbound queues are bounded. These are source observations, not a completed
adversarial network soak or proof that all remote resource problems are absent.

## Local verification, 2026-11-11

Base: `e17d588324a04709024b4d8cdbcde21841364448`, tree
`a48c1329635acbb85b93afe5320f4fb1e47d1673`.

All executed tests used Go 1.26.10, GOMAXPROCS=2, `-buildvcs=false -mod=readonly
-p=1`, GOTOOLCHAIN=local, GOWORK=off, GOENV=off, GOPROXY=off and GOSUMDB=off.
Each authorized phase held the shared test lock, used a finite outer timeout,
and retained intent, stdout/stderr, exit status and completion record. No failed
command was automatically retried.

1. **Negative control: expected failure, exit 1.** On the unchanged base plus only
   TestNodeCloseBeforeStartIsTerminal, `go test ./internal/relay -run
   '^TestNodeCloseBeforeStartIsTerminal$' -count=1 -timeout=90s` failed in 0.027s:
   `Start succeeded after Close; later Close cannot stop the server`. The test
   directly called server.Stop as safety cleanup. The old test source SHA-256 is
   `bd339fa8c6cffc6885471a7bc30d7dc2d2efae8d4ef4ce949fbb144061ab5a24`.
2. **Focused regression/race: pass, exit 0.** `go test -race ./internal/relay -run
   '^TestNode(Close|Duplicate|Failed|Concurrent)' -count=10 -timeout=150s` passed in
   2.022s. Each repetition covers terminal Close before/after Start, duplicate
   Start preserving the live listener, occupied-loopback-port startup failure
   and actual database-closed error, a fresh replacement Node, and 16 iterations
   of simultaneous Start with four Close callers. Successful listeners are
   rebound after shutdown to check release. The focused-run source hashes were:
   - node.go: `4811996fda2db8632e56e41b897d556e7f63e4d9dfbd1c659d16db69f01f6d05`
   - lifecycle_test.go: `a1b246beb9087d56334352d2d149b49c39347d6f18e591c38a06272bf6b75c16`
   A subsequent comment-only clarification describes the upstream Stop timeout
   more precisely; no implementation or regression code changed after this run.

At this initial record, full default checks, official-txpool tagged checks,
fuzzing, fresh vulnerability scanning and hosted CI have **not** been rerun on
this change. No transactions or transaction signing fixtures were used by the
focused lifecycle tests. No external BSC, RPC, private keys or deployment were
used. Broader verification must be recorded separately rather than inferred from
this focused pass. The [production gates](PRODUCTION_READINESS.md) remain open.

### Documentation contract follow-up

The first operator-documentation check on local candidate
`acb3c0251316c6121b1e78b20f3107d427c309bb` exited 1 because the checker also
requires documentation for the newly declared ErrNodeClosed string. The stats
checker passed. The 43-test Python suite exited 1 with four failures from that
same missing operator-documentation string. These failures were retained.

The operator runbook now documents that string in a separate lifecycle-callers
section, explicitly outside the HTTP rejection table and not as a Submit error.
No checker exclusion, Go implementation, test logic or HTTP contract was changed
to address the documentation failure.

After this documentation-only correction, both documentation checkers passed
and all 43 Python tests passed (exit 0). This does not replace the broader Go,
process-smoke and tagged-integration verification still pending at this record.

## Independent review and aggregate local verification

The independent reviewer found no blocking issue in the scoped lifecycle repair
at `acb3c0251316c6121b1e78b20f3107d427c309bb`. A separate authorized run of the
same focused race command with `-count=25 -timeout=95s` passed, exit 0, in 2.879s.
That covers 400 iterations of one Start racing four Close callers in addition to
the other lifecycle cases. It does not exercise malicious peers or the unverified
startup boundary above. The reviewer then checked the documentation-only delta
at `a7c2d0d0b07eb42d15f0faabd16ab62b8f8b8354`; the implementation and test hashes
remained identical to the independently tested source.

Both subsequent aggregate phases ran serially under the same exclusive test lock
on clean source `a7c2d0d0b07eb42d15f0faabd16ab62b8f8b8354`, tree
`a9ba233bb8130ffeb6511ca60b05b44354726218`. Their complete commands, source hashes,
intent, output, exit and timing were retained. This section is a later
verification record; it supersedes the earlier record's pending status only for
the checks explicitly completed below.

1. **Default checks and process smoke: PASS, exit 0.**
   `timeout --signal=TERM --kill-after=5s 300s make check smoke TEST_TIMEOUT=180s`.
   Started 05:28:13 UTC and finished 05:30:23 UTC on 2026-11-11. This ran default
   vet, all default packages under the race detector, build, 43 Python tests,
   the gofmt gate, `go mod verify`, the runtime dependency guard and real binary
   process smoke. The race results included checkpointproof 43.803s,
   compatcheck 1.145s, evidence 1.090s, firstepoch 37.961s and relay 5.262s.
   Module verification reported all modules verified. The process smoke checked
   start, health, no-peer readiness, stats identity/schema/headers and exact
   malformed-input validation increment, shutdown, and no data files.
2. **Exact official-txpool tagged scope: PASS, exit 0.**
   GOFLAGS was `-buildvcs=false -mod=readonly -p=1
   -modfile=integration/officialtxpool/go.mod -tags=officialtxpool`.
   A 240-second outer timeout ran, in order, `python3
   scripts/check_runtime_dependencies.py`, `go vet ./internal/relay`, and
   `go test -race -count=1 -timeout=180s ./internal/relay`, stopping on failure.
   Started 05:30:23 UTC and finished 05:31:16 UTC. The tagged relay package passed
   in 40.747s. This includes the existing isolated official BSC backend/txpool
   fixture and its real local RLPx reconnect. Its predictable public fixture keys
   and synthetic chain/balances are not user credentials, live funds, public
   BSC transactions, RPC use, or production-fork acceptance evidence.

The aggregate runner exited 0 and released the lock after both phases. No Go
source, tests or dependencies changed after those checks. Final tested file
SHA-256 values:

- node.go: `5dd23ed9ce36327c87aad09a2bac3f0da9b647775277590b8fe416f0f6ced563`
- lifecycle_test.go: `a1b246beb9087d56334352d2d149b49c39347d6f18e591c38a06272bf6b75c16`

Retained output log SHA-256 values, including the original failures:

| Evidence | Exit | SHA-256 |
| --- | --- | --- |
| Old lifecycle negative control | 1, expected | `88b881668731fb17efd7f2d9c30142939d71e17b639c34beabda81a675cee352` |
| Author focused race, 10 repetitions | 0 | `5789212896700af9d69fb9f53b8816bf6a32ee752abe72894ff706bd5ba6a90a` |
| Independent focused race, 25 repetitions | 0 | `25e4143176b31c5091d6f01585530c457d1f91869943b4fdf95c7ba5f1fb48ca` |
| Initial operations checker | 1 | `9e6e7eca9732f0b36b226bd9506846e79ec7bfee3f4ea07236ccd4606b3c2ce2` |
| Initial 43-test Python suite | 1 | `6734ebac100fc4826d66a347b9583df03589a40d50f4e02af81d65b416a4ee20` |
| Initial stats checker | 0 | `36fb522b6cb6004f3722ca1a9a0c29298829facb7d321364b13e91e21d959420` |
| Corrected docs checkers and Python suite | 0 | `afb337807dc77deb2951f59534201e5d312a51d40ab6aa2d3f52932e43cac0b3` |
| Default checks and process smoke | 0 | `cf7ea24c12b65046669d5e34859edb38d88e999b74b6934c522f9ddc794c692a` |
| Exact official-txpool guard/vet/race | 0 | `95ef89bcf5395e8ba4b107c26020b6a2a9f444162cb0027bd92e3168fe7a3443` |

**Still not executed for this change:** fresh vulnerability scans, new fuzzing,
the separate canonical-isolation tagged research suite, hosted CI, UDP failure
interleaving, public-peer experiments, long-duration adversarial network soak or
deployment. Canonical-isolation runtime sources are unchanged; their older
results remain historical, not a fresh run for this commit. This work does not
consume or endorse the separately unapproved complete Prysm dependency candidate.
Before any later authorized publication is called verified, all three hosted
jobs must actually pass on the exact published commit, including their real
scanner scopes with fresh advisory data. There was no public publication in this
work, and no production or vulnerability-scan pass is inferred from these local
checks.
