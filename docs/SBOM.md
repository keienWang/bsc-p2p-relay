# Software bill of materials

`SBOM.cdx.json` is a CycloneDX **1.6** JSON manifest for the Go application at public source commit `954dc237fd2e6ed3d96baba1c6a90273d4c85341`:

https://github.com/keienWang/bsc-p2p-relay/commit/954dc237fd2e6ed3d96baba1c6a90273d4c85341

That commit passed [hosted test/security workflow run 42846131226](https://github.com/keienWang/bsc-p2p-relay/actions/runs/42846131226) before this manifest was generated. The manifest describes Linux amd64 with CGO enabled and Go 1.26.10. It includes Go runtime/application module dependencies, not every operating-system or native-library package, test-only dependency, a container image, or a released binary. Recreate it for each supported target/release.

## Generation and provenance

The generator is pinned to `github.com/CycloneDX/cyclonedx-gomod/cmd/cyclonedx-gomod@v1.9.0`. `scripts/generate_sbom.py` copies the exact selected Git commit into a disposable ordinary local clone, so linked-worktree limitations do not change which source is described. It does not read uncommitted edits, change the source checkout, fetch a different branch, or push anything.

Before running the generator, the script requires this repository’s HTTPS origin and root module identity, rejects symlinks/submodules and unexpected local module replacements, and verifies that the selected commit really replaces STUN v2 with the in-tree `compat/stunv2` module. Old revisions without that adapter are rejected rather than relabeled. This is a provenance consistency check, not proof that a local commit is published or trustworthy.

The small post-processing step accurately identifies the project-owned `compat/stunv2` component. Its Go import path remains `github.com/pion/stun/v2`, but it is not the obsolete upstream STUN v2 implementation. Its component name, generic package URL, source reference, license and dependency references identify this repository's adapter instead. The root application and adapter license are asserted as GPL-3.0-or-later. Other third-party license classifications are intentionally not guessed by an automatic detector; the complete dependency license audit remains an open release gate.

The JSON was validated against the official CycloneDX 1.6 JSON schema with its SPDX and JSON Signature Format references. This confirms schema conformance, not dependency safety or completeness of a legal audit.

## Reproduce

From a checkout containing the desired committed revision:

```
python3 scripts/generate_sbom.py --commit COMMIT_SHA --output docs/SBOM.cdx.json
```

Or pass `--source /path/to/checkout` for another local checkout. `make sbom` uses HEAD. For a published manifest, first verify that the selected source commit is actually available in the public repository. A local development SHA is not automatically public. Uncommitted changes are not represented. Python 3, Git, a trusted Go toolchain and access to the public Go module registry are required. The source checkout must have an HTTPS `origin` for this repository.

Generation fixes `GOWORK=off`, `GOENV=off`, module mode and read-only module loading. Custom `GOFLAGS` (except `-buildvcs=false`) and nonempty `GOEXPERIMENT` are rejected, so overlays, alternate module files, vendor mode and custom build tags cannot silently change the source graph. Target `GOOS`, `GOARCH` and `CGO_ENABLED` remain supported and are recorded in the manifest. Before writing output, the script checks reference uniqueness/closure and rejects filesystem locations, credential-bearing URLs and URL queries/fragments. It does not sandbox malicious toolchains or verify that the public source passed CI; those are separate release checks.

`make python-test` validates repository/adapter identities, environment isolation, reference rewriting/closure and output location safeguards without contacting the network. Generation metadata and tool binary hashes can differ by tool environment/time. The selected source commit and target build properties are the primary provenance anchors.

A passing vulnerability scan, this manifest, and its valid schema do not imply production readiness. Keep the independent network, consensus/header, supply-chain audit and release gates in PRODUCTION_READINESS.md.
