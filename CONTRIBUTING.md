# Contributing

This project is experimental. Do not use production keys, balances or signed mainnet transactions in tests. Use deterministic test-only keys and loopback peers with discovery disabled. Never copy private application code or credentials into changes.

Use Go 1.26.11 or a newer supported security-patched version. Run `make check`, `make smoke`, `make fuzz`, and `make vuln`. A failing vulnerability scan is a release blocker until the finding is fixed or independently reviewed and documented with an explicit acceptance decision. Do not silently suppress it. Record exact tool versions and exit codes in verification notes.

Keep changes small and purposeful. Every bug fix should have a regression test that exercises the layer where the bug occurred. Separate queue admission, transport write, peer acceptance, inclusion, execution, and finality in names and API contracts. Do not replace a missing verification step with an optimistic success flag.

Protocol changes must cite pinned official BSC source, preserve license notices, and include cross-version interoperability tests. Do not change wire capabilities unless the complete negotiated handshake and messages are implemented. Changes to API exposure, wallet handling, persistence, public networking in tests, or verification trust assumptions require explicit design review.

No pull request may claim production readiness solely from unit tests, code coverage, or a clean scanner. See `docs/PRODUCTION_READINESS.md`.
