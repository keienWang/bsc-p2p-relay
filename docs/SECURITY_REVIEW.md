# Security review record

Date: 2026-11-10. This is an engineering review record, not an external audit or production approval.

## Current dependency follow-up

The relay keeps BSC v1.8.2 and the reviewed STUN v3 adapter. A follow-up, isolated full-backend integration test made a gRPC server path visible to test-inclusive scanning. That finding was not suppressed even though the fixture disables RPC/MEV listeners. The root module and adapter module now pin the following upstream updates:

- `google.golang.org/grpc v1.83.2`: GO-2026-6443.
- `golang.org/x/net v0.62.0`: GO-2026-6866 and GO-2026-6860.
- `golang.org/x/crypto v0.58.1-0.20261009181718-2f1f834330b2`: GO-2026-5932. This is the upstream module-split commit `2f1f834330b2d36ab3db8bfb33aaa1ac16794588` ("openpgp: carve out openpgp as a module"), not an OpenPGP algorithm fix or stable release tag. It removes the unused, unmaintained OpenPGP package from the parent module. The new standalone `golang.org/x/crypto/openpgp` module remains affected in all versions and must not be introduced. The dependency guard rejects its module and package paths in both the relay and default test dependency graphs. At selection time the public parent-module version list ended at v0.58.0; reconsider the pseudo-version when a containing stable release is available.

Minimum-version selection also updates x/sync, x/sys, x/text and x/term. `go mod tidy` records the Windows-only `github.com/Microsoft/go-winio v0.6.2` dependency; this is not a claim that Windows has been tested. The root and nested adapter module checksums verify successfully.

After these changes, `govulncheck@v1.9.0 ./...` exits 0 with `No vulnerabilities found.` and no additional imported-package/module notices for its analyzed graph. This does not certify every module in the entire upstream module graph or establish production readiness. No scanner exemption, hidden failure or continue-on-error was added. The separate full-backend test graph and test-inclusive scan must be reviewed independently before that test is merged.

Local verification with Go 1.26.10: `make check smoke vuln` passes (format/vet, complete race suite, build, Python regression checks, module verification, runtime dependency guard, process smoke and scanner). The nested adapter module also passes `go mod verify`. Hosted CI must still be checked for the exact published commit.

The historical SBOM remains explicitly tied to public commit 954dc237 and is not a current or release manifest for these changes. Regenerate against a verified public source commit before a release.

## Earlier STUN remediation

The initial red GitHub run was caused by the DTLS v2 finding described below; formatting, vet, race tests, build and process smoke had passed. The STUN compatibility fix replaces BSC's obsolete STUN v2 implementation with a narrow adapter delegating to maintained STUN v3.1.7 / DTLS v3.1.5. The old DTLS v2 packages are absent from the application dependency list. No scanner exemption or continue-on-error was added.

At that earlier STUN-fix revision, the govulncheck v1.9.0 command exited **0**, reporting **0 vulnerabilities affecting called code**, while still noting 1 imported-package finding and 3 module findings without identified call paths. This is a passing symbol-level scan, not a claim that every transitive package has no advisory. See CI_SECURITY_FIX.md and compat/stunv2/README.md for scope, tests and limitations.

## Historical dependency scan

The initial Go 1.25.4 / BSC transitive dependency build produced 36 symbol-level vulnerability findings. The toolchain was replaced with official Go 1.26.10, verified against the Go download manifest SHA-256. Security-patched module versions were selected for `github.com/klauspost/compress` (1.20.2), `golang.org/x/net` (0.60.0), and `google.golang.org/grpc` (1.83.1), with their resolved supporting dependency upgrades recorded in go.mod/go.sum. BSC itself remains pinned at v1.8.2.

Before this remediation, the follow-up scan with `golang.org/x/vuln/cmd/govulncheck@v1.9.0` exited nonzero (scanner status 3): **GO-2026-4479**, `github.com/pion/dtls/v2@v2.2.12`, concerning random AES-GCM nonces. The vulnerability record lists no fixed v2 version. See https://pkg.go.dev/vuln/GO-2026-4479.

Source inspection found this dependency through BSC's NAT/STUN package. BSC's `p2p/nat/stun.go` uses STUN `Dial("udp4", ...)`; the reviewed STUN v2 DTLS session creation occurs in `DialURI` for TURN-over-DTLS. This relay does not configure NAT or call DialURI. Scanner traces were predominantly package initialization and string/error methods. These observations do not establish that a remotely triggerable DTLS path exists in this application, and they do not establish a clean scan. That review motivated the explicit STUN generation replacement above. No finding has been suppressed in CI.

That historical follow-up scan also reported findings in imported packages/modules without identified symbol-call paths. The subsequent changes are recorded above. Re-run the scan on the exact release commit; this snapshot is not a lasting security guarantee.

## Review fixes

- Global duplicate suppression prevented retry to replacement peers. Replaced with per-peer bounded, expiring suppression and added reconnection tests.
- A 2 MiB cap on every message could disconnect legitimate peers sending large blocks. Restored the official 10 MiB envelope cap, retained a smaller request-decoding cap, and added a runPeer-level large-block survival test.
- Queued transactions could outlive cache TTL. Added deadlines and rejection of expired or missing-deadline transaction queue items; added writer regression tests.
- Cache expiry may occur between cache admission and deadline lookup. Missing/expired deadlines now fail admission, and the writer independently rejects missing deadlines.
- Documentation now distinguishes HTTP 503 with no peer, HTTP 429 on capacity failure, and queued versus cached duplicate responses.

## Remaining scope

No public-network penetration test, extended soak, official full-node txpool acceptance test, complete license audit, or consensus/finality verifier has been completed. See PRODUCTION_READINESS.md for the complete gates.
