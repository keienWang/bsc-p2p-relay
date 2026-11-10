# Software bill of materials

`SBOM.cdx.json` is a CycloneDX **1.6** JSON manifest for the Go application at public source commit `3d3ef40c2481282cfe986eed4cba1db0fc0fae0f`:

https://github.com/keienWang/bsc-p2p-relay/commit/3d3ef40c2481282cfe986eed4cba1db0fc0fae0f

That commit passed both the default test/security job and the isolated official BSC txpool job in [hosted workflow run 42862935041](https://github.com/keienWang/bsc-p2p-relay/actions/runs/42862935041) before this manifest was generated. The manifest describes Linux amd64 with CGO enabled and Go 1.26.10. It includes Go runtime/application module dependencies, not every operating-system or native-library package, test-only dependency, a container image, or a released binary. Recreate it for each supported target/release.

The separate [targeted Herumi archive evidence](native-herumi/README.md) records one native archive's source associations and selected notices. It does not change this Go SBOM's coverage, cover other CGO/native components, or complete the dependency-license audit and corresponding-source requirements.

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

## Module inventory and refresh verification

`DEPENDENCIES.json` was also regenerated from a clean checkout of the same
public commit, using the existing `scripts/dependencies.py` with `GOENV=off`,
`GOWORK=off` and `GOFLAGS='-buildvcs=false -mod=readonly'`. It lists the **629
selected root modules**, including the main module and unused/transitive module
requirements. It is not a list of compiled application packages, and is not an
SPDX or CycloneDX SBOM. It does not use the separate official-txpool modfile;
that expanded integration-test graph has its own evidence under
`integration/officialtxpool/`. Regenerate and verify the inventory from the
intended clean source revision when making a release; running `make dependencies`
in another checkout describes that checkout's selected module graph instead.

For this 2026-11-10 refresh, the CycloneDX document contains the application plus
83 components: 81 external Go modules, the project-owned STUN adapter and the Go
standard library. All 81 external module hashes match the public source's
`go.sum`. The adapter hash was independently recomputed with Go's directory-hash
format and the generator's module-coordinate prefix. Schema validation against
the official 1.6 schema and its SPDX/JSF references reported zero errors, and
component references were unique and closed. The output passed checks for local
filesystem paths and credential-bearing URLs. No generator, root module or
production code was changed by this refresh.

These documents update the earlier manifest tied to `954dc237`; that earlier
source remains available in Git history. The current source link is an actual
published revision whose exact two-job workflow passed before generation, not a
local development SHA. The manifests still do not cover native OS/C library
packages, the alternative test graph, every supported target, or a released
binary. They do not resolve the separately recorded BLS concurrency issue.
