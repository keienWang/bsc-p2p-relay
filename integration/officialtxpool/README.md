# Isolated official BSC txpool integration test

This integration test embeds the pinned **BSC v1.8.2 official `eth.Ethereum`
backend**, its real P2P handler/fetcher, a state-backed `core.BlockChain`, and
its real transaction pools. It is not a mock pool, an official-binary deployment,
a public-peer test, or evidence of mainnet/testnet acceptance.

The build tag and alternative modfile isolate the full backend's larger test
dependency graph from the relay's default build. Ordinary `go test ./...` does
not run this test. The CI workflow defines a separate `official_txpool` job
without `continue-on-error`; it checks this integration graph independently.

## Run from the repository root

Use the repository's Go version (currently Go 1.26.10):

```sh
export GOFLAGS='-buildvcs=false -mod=readonly -modfile=integration/officialtxpool/go.mod -tags=officialtxpool'
python3 scripts/check_runtime_dependencies.py
go vet ./internal/relay
go test -race -count=1 -timeout=180s ./internal/relay
```

BSC imposes a 35-second redial cooldown, so the integration test takes about
35 seconds. The test waits for that cooldown and never changes or bypasses it.
Earlier reconnect stress testing passed three repetitions under the
race detector. Routine CI runs the relay package once, without repeating the
unrelated, expensive first-epoch consensus suite.

The modfile is an alternative for the **same root module**, not a standalone
module to run from this directory. Its `./compat/stunv2` replacement must resolve
to this checkout's existing compatibility module:

```sh
go list -mod=readonly -modfile=integration/officialtxpool/go.mod \
  -m -json github.com/pion/stun/v2
```

## What is asserted

1. The official backend runs with a private synthetic Parlia genesis and funded
   public fixture key on chain ID 13371337. Its explicit `SetSynced()` call only
   enables transaction handling for this genesis-only chain.
2. Both real RLPx listeners bind only `127.0.0.1`. Discovery v4/v5, DNS discovery,
   NAT and official-node dialing are disabled. Both peers have a loopback
   `NetRestrict`, no configured bootstrap/static/trusted nodes, and the relay
   explicitly adds only the official node's live private peer record.
3. HTTP, WebSocket, authenticated RPC and IPC listeners are all absent, and
   MEV gRPC is disabled. No RPC client or RPC call is used. The official backend
   internally registers APIs, as it always does, but opens no RPC endpoint.
4. Signed legacy, access-list and dynamic-fee transactions enter the official
   **pending pool**, with their exact hashes/types retained. This goes beyond
   observing relay queue admission or P2P receipt.
5. A same-session duplicate is suppressed by the relay. A valid unfunded
   transaction enters the relay queue but is absent from the official pool
   after a subsequent valid transaction is accepted on the same ordered
   connection. Only then is the rejected fixture passed directly to the real
   pool to confirm `ErrInsufficientFunds`, since eth P2P exposes no rejection
   response. **No accepted transaction is inserted directly by the test.**
6. A transient `DiscNetworkError` causes the same static peer to reconnect.
   A cached duplicate is retried on the new session, remains unique in the
   official pool, and a new transaction is accepted. `RemovePeer` or
   `DiscRequested` is deliberately not used: BSC remembers an explicit
   disconnect request and refuses that identity, unlike a transient failure.
7. The final pool contains exactly five executable transactions from the funded
   fixture account, with contiguous nonces and none from the unfunded account.
   The chain remains at genesis and mining is never started.
8. Cleanup closes the relay, official backend, databases and listener lifecycles,
   and Go removes all fixture data under `t.TempDir()`.

## Safety and scope

The private keys `1` and `2` are public, predictable test fixtures, not user
credentials. Their synthetic balance exists only in the isolated genesis. Never
fund those addresses or use these keys for a public network. The fixture is
selected through package-private test access; no production network override,
private key flag or signing path is added to the relay.

This test does not validate real BSC mainnet state, current production fork
conditions, validator operation, execution/inclusion/finality, public-peer
retention, synchronization, the official executable's packaging, or deployment
configuration. Local txpool interoperability is one acceptance step; the
separate production gates remain open.

## Separate dependency and security scope

Importing the full official backend also builds upstream node, miner, Prysm
keymanager, storage and telemetry code. The alternative modfile preserves every
existing root requirement at the same version and adds 84 explicit indirect
requirements. Relative to the documented comparison baseline, it adds 410
non-standard imported package paths; selected-module enumeration expands from
629 to 744 entries including the main module. Only the transitive selected
Gomega version differs (1.10.1 to 1.31.0). Exact graph differences and versions
are under `evidence/`; graph-only entries are not necessarily built or reachable.

The integration graph inherits the root's gRPC 1.83.2 and x/net 0.62.0 security
patches. Its x/crypto 0.58.1-0.20261009181718-2f1f834330b2 pseudo-version removes
the unmaintained OpenPGP packages from the parent module by splitting them out.
**That split does not repair OpenPGP's algorithms or make OpenPGP safe.** The
separate `golang.org/x/crypto/openpgp` module remains affected by GO-2026-5932
across its versions and must not be reintroduced. The dependency guard rejects
OpenPGP package imports and the split module, even as a graph-only dependency;
it is also run with this test's tag and alternative modfile in CI.

Install the pinned scanner independently, without the integration GOFLAGS, then
scan the actual integration test graph:

```sh
GOFLAGS=-buildvcs=false go install golang.org/x/vuln/cmd/govulncheck@v1.9.0
GOFLAGS='-buildvcs=false -mod=readonly -modfile=integration/officialtxpool/go.mod -tags=officialtxpool' \
  govulncheck -test -tags=officialtxpool -show=verbose ./internal/relay
```

The scanner executable must be on `PATH`. The CI job installs it in an isolated
tools directory and invokes it directly. `-test`, the build tag and the modfile
are essential: a default production-only scanner pass does not cover this
fixture. Do not suppress findings or relax either scan. When root dependencies
change, refresh and review this modfile, then rerun both scopes independently.
See [the verification record](VERIFICATION.md) for commands, results and limits.
