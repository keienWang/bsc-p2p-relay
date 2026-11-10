# BSC STUN compatibility adapter

BSC v1.8.2's only `github.com/pion/stun/v2` consumer is `p2p/nat/stun.go`. It uses a small API surface: DefaultPort, Dial, Build, TransactionID, BindingRequest, Event and XORMappedAddress. The obsolete STUN v2 module imports DTLS v2, affected by GO-2026-4479 with no patched v2 release.

This local replacement delegates exactly that API to maintained `github.com/pion/stun/v3 v3.1.7`, which depends on `github.com/pion/dtls/v3 v3.1.5`. It does not copy, stub out or reimplement STUN or cryptography. It is not a general drop-in STUN v2 replacement. An added top-level v2 symbol not exported here will fail compilation. The Client type alias exposes upstream v3 methods, so compilation alone cannot detect every API change; every BSC upgrade requires a deliberate adapter review.

The root integration test exercises the real BSC NAT/STUN consumer against a loopback UDP responder. Normal relay operation still does not configure automatic NAT or a STUN server. Dependency scanning remains enabled without exemptions.

Adapter code is GPL-3.0-or-later; the upstream Pion implementation retains its MIT license. Keep these distinctions in notices and release source bundles. Remove this adapter when pinned BSC uses a maintained STUN generation directly.

This adapter preserves upstream request/timeout/close behavior. It does not promise that Close cancels an in-flight blocking Do; BSC's caller closes after Do completes. New cancellation semantics are outside this adapter's scope.
