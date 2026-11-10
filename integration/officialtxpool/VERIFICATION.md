# Verification of the isolated official BSC txpool integration test

Date: 2026-11-10 UTC. Platform: Linux amd64. Go: 1.26.10.
The integration fixture uses the official BSC v1.8.2 backend and real txpool.
This is a local integration-test record, not production certification or evidence
of a published release. The dependency comparison baseline is local source
revision `5dc163242abce1b9ca90fa74100fdf1e7631f1e6`, whose tree matches the
[public dependency baseline](https://github.com/keienWang/bsc-p2p-relay/commit/37f461ad0af5bbbf50c38861f9220476b5cc04a8).
Local and public commit identifiers differ; only their source trees are equivalent.

## What the result establishes

The real eth/68 handler and stateful txpool accept five fixture transactions
exclusively through relay-to-backend loopback RLPx. The test covers legacy,
access-list and dynamic-fee transactions, same-session deduplication, balance
rejection, transient disconnect/reconnect, and duplicate retry after reconnect.
The backend remains at its unmined synthetic genesis. Every listener is confined
to IPv4 localhost; discovery, DNS, NAT, external peers and RPC/MEV gRPC endpoints
are absent. Test keys are public fixtures and state uses a temporary directory.
No public-network transaction is sent.

This does not establish official-binary deployment, current mainnet/testnet
state/fork acceptance, synchronization, inclusion, execution or finality.

## Original batch verification

All checks below passed with exit 0. The tagged official-pool fixture
took 35.60 seconds (relay package 38.093 seconds); the default first-epoch suite
ran once and took 63.284 seconds. The current tagged scan reports no
vulnerabilities and no module-only notices.

Commands run from the repository root with Go 1.26.10.

1. Default suite and build checks, with only `GOFLAGS=-buildvcs=false`:
   `make check`.
   Evidence: `evidence/default-make-check.txt` and `.exit`.
2. Set the integration environment:
   `GOFLAGS='-buildvcs=false -mod=readonly -modfile=integration/officialtxpool/go.mod -tags=officialtxpool'`.
   Under that environment, run these consecutive checks:
   - `python3 scripts/check_runtime_dependencies.py`
   - `go vet ./internal/relay`
   - `go mod verify`
   Evidence: `evidence/tagged-guard-vet-verify.txt` and `.exit`.
3. With the same integration environment:
   `go test -race -count=1 -timeout=180s -v ./internal/relay`.
   Evidence: `evidence/tagged-relay-race.txt` and `.exit`.
4. Install the scanner separately using an isolated tools directory and
   `GOFLAGS=-buildvcs=false`: `go install golang.org/x/vuln/cmd/govulncheck@v1.9.0`.
   Then, using the exact integration environment from step 2:
   `govulncheck -test -tags=officialtxpool -show=verbose ./internal/relay`.
   Evidence: `evidence/govulncheck-current.txt` and `.exit`.
5. Verify unchanged root modules and production source with `git diff`, and
   verify the alternative local replacement using:
   `go list -mod=readonly -modfile=integration/officialtxpool/go.mod -m -json github.com/pion/stun/v2`.
   Its directory must be this checkout's `compat/stunv2`, not a directory
   relative to the alternative modfile. The recorded replacement metadata uses
   `${REPOSITORY_ROOT}` instead of a machine-specific path.

The default `make check` runs the first-epoch consensus suite once. The tagged
job targets only the relay and does not rerun that unrelated expensive suite.
Earlier reconnect stress testing of the same fixture passed three repetitions
under the race detector. `evidence/previous-reconnect-race-x3.txt` preserves that
historical result; it predates this comparison baseline and is not a substitute
for the current checks above. BSC's normal redial cooldown accounts for roughly
35 seconds per fixture. The test never shortens that cooldown or resets a
blocklist. Explicit `DiscRequested` is not equivalent to a transient failure
because BSC retains that identity; the test uses `DiscNetworkError`.

## Integration with the checkpoint research baseline

The final tree combines this unchanged integration implementation and alternative
module files with local `af68f2e9098e9100af1bae946aa4c00fbe665fdb`, whose tree is
published as [b5c8f892](https://github.com/keienWang/bsc-p2p-relay/commit/b5c8f8922e33e8f25a02fa1d0f4aaf753e2fab28).
All checkpoint proof files, the finite `TEST_TIMEOUT=300s` Makefile default and
the known BLS cache diagnostic/blocker remain intact. The original default CI
job is unchanged. This integration does not resolve that BLS race.

Final local integration checks, all exit 0:

- `GOFLAGS='-buildvcs=false -p=1' make check smoke vuln`: complete default race
  suite, build, 10 Python tests, formatting/module verification, dependency
  guard, process smoke and vulnerability scan. Checkpoint proof took 25.101s and
  first-epoch verification 62.817s. See `evidence/integrated-default.txt` and `.exit`.
- The same tagged relay race command above: 36.830s for the package, 35.22s for
  the official txpool fixture. Tagged guard, vet and module verification also
  passed after integration.
- Independently installed `govulncheck` v1.9.0, with the same integration
  `GOFLAGS` and `-test -tags=officialtxpool ./internal/relay`: no vulnerabilities
  or extra notices. See `evidence/integrated-vuln.txt` and `.exit`.

These checks exclude the explicitly failing `blscacherace` diagnostic. Default
CI for the public checkpoint-only baseline passed in [run 42861040864](https://github.com/keienWang/bsc-p2p-relay/actions/runs/42861040864),
but that earlier run did not contain this official txpool job. Hosted validation
of the combined revision remains a separate requirement below.

## Dependency comparison and OpenPGP boundary

Relative to the comparison baseline, the alternative modfile retains every root
requirement at the same version and adds 84 explicit indirect requirements.
It adds 410 non-standard imported package paths to the relay test graph.
Selected-module enumeration is 629 versus 744 entries including the main module,
with Gomega 1.10.1 → 1.31.0 the only changed selected version. Exact versions are
in `evidence/added-requirements.txt`, `added-imports.txt` and
`module-graph-diff.txt`. Graph-only dependencies are not necessarily built.

The x/crypto pseudo-version ending `2f1f834330b2` is the **OpenPGP module split**,
not an OpenPGP algorithm repair. It removes unused and unmaintained OpenPGP code
from the parent module. The separate `golang.org/x/crypto/openpgp` module remains
unsafe and affected across its versions; reintroducing it is prohibited. The
actual tagged/alternative-modfile guard checks both compiled relay/test package
graphs and selected modules, rejecting OpenPGP and the split module. A scan
becoming clear after the split is not evidence that OpenPGP itself became safe.

## Historical scan evidence, from older dependency graphs

The following two raw reports were generated before the current root dependency
baseline and are preserved only to explain the dependency investigation. They
are not scans of today's root graph. Each used govulncheck v1.9.0 with the
`officialtxpool` tag, alternative modfile and `-test`; the baseline report traces
`TestOfficialBSCTxPoolLoopback` through `node.Node.Start` into gRPC server code.

- `govulncheck-before-fix.txt`, exit 3: reachable GO-2026-6443 in gRPC 1.83.1,
  plus module-only GO-2026-6866/6860 in x/net 0.60.0 and GO-2026-5932 in
  x/crypto 0.57.0. This is the full pre-change rerun after isolation assertions.
- `govulncheck-grpc-only.txt`, exit 0: gRPC 1.83.2 removed the reachable finding,
  but the three module-only notices remained. Those notices were not suppressed.

The old advisory's reported x/crypto “Fixed in” version identifies removal by
module split, not cryptographic remediation. The current scan and explicit
OpenPGP guard must both pass; no prior exit status substitutes for either.

## CI status

The workflow adds an unconditional separate `official_txpool` job using the
existing pinned checkout/setup-go action SHAs and Go 1.26.10. The job preserves
the same tag/modfile for the dependency guard, relay vet, one race-enabled relay
run and `govulncheck -test`. Scanner installation is independent of that module
environment. The original default CI job remains unchanged, and neither job
uses `continue-on-error`.

Local YAML/command checks confirmed the unchanged default job, pinned action
versions, unconditional integration steps and exact tag/modfile environment
(`evidence/ci-structure.txt`). Hosted CI was not exercised by these local commands. The publisher must verify
the workflow on the exact published revision before claiming hosted CI success.
