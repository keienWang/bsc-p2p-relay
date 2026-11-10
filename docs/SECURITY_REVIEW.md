# Security review record

Date: 2026-11-10. This is an engineering review record, not an external audit or production approval.

## Current STUN remediation

The initial red GitHub run was caused by the DTLS v2 finding described below; formatting, vet, race tests, build and process smoke had passed. The STUN compatibility fix replaces BSC's obsolete STUN v2 implementation with a narrow adapter delegating to maintained STUN v3.1.7 / DTLS v3.1.5. The old DTLS v2 packages are absent from the application dependency list. No scanner exemption or continue-on-error was added.

The post-fix govulncheck v1.9.0 command exits **0**, reporting **0 vulnerabilities affecting called code**, while still noting 1 imported-package finding and 3 module findings without identified call paths. This is a passing symbol-level scan, not a claim that every transitive package has no advisory. See CI_SECURITY_FIX.md and compat/stunv2/README.md for scope, tests and limitations.

## Historical dependency scan

The initial Go 1.25.4 / BSC transitive dependency build produced 36 symbol-level vulnerability findings. The toolchain was replaced with official Go 1.26.10, verified against the Go download manifest SHA-256. Security-patched module versions were selected for `github.com/klauspost/compress` (1.20.2), `golang.org/x/net` (0.60.0), and `google.golang.org/grpc` (1.83.1), with their resolved supporting dependency upgrades recorded in go.mod/go.sum. BSC itself remains pinned at v1.8.2.

Before this remediation, the follow-up scan with `golang.org/x/vuln/cmd/govulncheck@v1.9.0` exited nonzero (scanner status 3): **GO-2026-4479**, `github.com/pion/dtls/v2@v2.2.12`, concerning random AES-GCM nonces. The vulnerability record lists no fixed v2 version. See https://pkg.go.dev/vuln/GO-2026-4479.

Source inspection found this dependency through BSC's NAT/STUN package. BSC's `p2p/nat/stun.go` uses STUN `Dial("udp4", ...)`; the reviewed STUN v2 DTLS session creation occurs in `DialURI` for TURN-over-DTLS. This relay does not configure NAT or call DialURI. Scanner traces were predominantly package initialization and string/error methods. These observations do not establish that a remotely triggerable DTLS path exists in this application, and they do not establish a clean scan. That review motivated the explicit STUN generation replacement above. No finding has been suppressed in CI.

The follow-up scan also reports findings in imported packages/modules without identified symbol-call paths. Re-run the scan on the exact release commit; this snapshot is not a lasting security guarantee.

## Review fixes

- Global duplicate suppression prevented retry to replacement peers. Replaced with per-peer bounded, expiring suppression and added reconnection tests.
- A 2 MiB cap on every message could disconnect legitimate peers sending large blocks. Restored the official 10 MiB envelope cap, retained a smaller request-decoding cap, and added a runPeer-level large-block survival test.
- Queued transactions could outlive cache TTL. Added deadlines and rejection of expired or missing-deadline transaction queue items; added writer regression tests.
- Cache expiry may occur between cache admission and deadline lookup. Missing/expired deadlines now fail admission, and the writer independently rejects missing deadlines.
- Documentation now distinguishes HTTP 503 with no peer, HTTP 429 on capacity failure, and queued versus cached duplicate responses.

## Remaining scope

No public-network penetration test, extended soak, official full-node txpool acceptance test, complete license audit, or consensus/finality verifier has been completed. See PRODUCTION_READINESS.md for the complete gates.
