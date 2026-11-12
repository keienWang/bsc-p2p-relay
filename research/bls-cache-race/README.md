# BLST cache race: patch and blackbox research material

Status: preparation only. The six new Go tests and this cache patch have not
been compiled or run as a comparison. No race-freedom, native-safety or
production-readiness result is claimed. Review is required before a future
runner can use these materials.

## Included material

- `0001-use-bounded-synchronous-blst-cache.patch`: a two-file diff against
  fixed Prysm v5.3.2. It retains the two-million-entry bound, point validation
  and copy-on-return behavior; it changes cache synchronization/recency behavior.
- `blackbox_test.go.txt`: six public-API tests, stored as inert text rather than
  an automatically discovered Go test. It imports only Prysm BLST and standard
  library packages; its transitive source/native graph still needs review.
- [UPSTREAM.json](UPSTREAM.json): fixed sources, hashes and public-point derivation.
- [NOTICE.md](NOTICE.md): source and change notices; [LICENSE](../../LICENSE)
  supplies the repository GPL version-3 text.
- [MANIFEST.json](MANIFEST.json): exact material identities and six test names.

No complete source fork, dependency implementation, native binary, four-test
internal/whitebox suite, test-install patch or execution wrapper is included.
No module file, workflow, runner, application code or selected dependency is
changed by adding this directory.

The unchanged fixture comment names `PROVENANCE.json`; this distribution records
its public-coordinate provenance in `UPSTREAM.json` instead. No unavailable
record is needed to locate the cited public coordinate source and derived seed.

## Future controlled comparison, not an executable setup

Both sides must use separately prepared **private local replacements** of the
same verified Prysm commit. Each derived module/test directory receives these
exact blackbox bytes at `cachepatchtests/public_key_cache_blackbox_test.go`.
The original-cache side receives no production patch. The candidate side also
receives `0001-use-bounded-synchronous-blst-cache.patch`. Neither side is a
public fork or is supplied by this directory.

Use the same reviewed harness, toolchain, native/link settings, source checks,
resource/output limits and exact test selection on both sides. Use fresh test
processes for cold-target comparisons. Capture original and patched outcomes
separately; a candidate pass alone does not establish a reproduced baseline
race, and a quiet baseline run does not prove the race absent.

The main module must remain the **verified relay root**, not Prysm. Future private
`.mod`/`.sum` copies must preserve the fixed relay module identity, requirements
and existing replacements, adding only the intended Prysm local replacement.
Keep the replacement module's own Prysm identity. Run from the relay root so
its relative replacements still resolve correctly. The package argument is
`github.com/prysmaticlabs/prysm/v5/cachepatchtests`, not `./cachepatchtests`.
Do not use the relay modfile as a Prysm main-module definition.

Before execution, bind relay/modfile/source hashes, verify the selected Prysm
replacement and BLST version in `UPSTREAM.json`, and inspect the actual compiled
package/native source graph in that same context. Prysm-root defaults and relay
selection are not interchangeable. Do not silently change requirements or fetch
missing inputs. Use only a separately authorized available toolchain/dependency
set; local-toolchain and offline module-resolution settings are not OS network
isolation. No replace directive or private modfile is created by this material.

To claim a candidate pass, its JSON test stream must name the expected package
and contain RUN and PASS for **all six exact test names** in `MANIFEST.json`,
plus a final package PASS and zero exit. Zero matches, a missing test, a skip,
a failure, a truncated stream or a changed source/hash fails the candidate gate.
The same harness must capture complete, source-bound evidence for the control:
an observed original-cache race failure is that control's result, not a passing
test run. A build/setup failure is not a reproduced cache race. No current
runtime receipt exists.

## Public-only fixtures and remaining boundaries

The fixture parses one fixed public seed through Prysm once, requires round-trip
encoding, and keeps it read-only. Copy/aggregate binary doubling constructs
public multiples without parsing the target points first. Only the seed may
already be cached; “cold target” does not mean an entirely empty global cache.
The fixture byte registry never locks the cache calls under test. No secret
object creation/parsing, signing, account, transaction or network API is used
by these new test bodies.

The seed bytes are derived from cited public affine coordinates, not a directly
inspected upstream compressed-vector record. This defines a new public-point
fixture family; no equivalence to older secret-derived fixture bytes is claimed.
Go/native seed validation, round-trip, point uniqueness and concurrent behavior
remain unrun. The tests are not a cryptographic validation suite, exhaustive
source/license closure, performance budget or native-safety proof. Capacity,
contention, memory, other callers/build graphs and remaining validation gates
still require separate work. The generic nonblocking-cache package is unchanged.
