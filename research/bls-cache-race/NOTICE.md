# Scope and notices

## Prysm patch and new tests

`0001-use-bounded-synchronous-blst-cache.patch` is a proposed modification of
Prysm v5.3.2 at commit `21e1f7883b88ce39d16263d1cdea70bfdaa4d359`.
It changes only `crypto/bls/blst/init.go` and `crypto/bls/blst/public_key.go`:
replace the asynchronous cache with the existing dependency's synchronized
bounded LRU, and adapt cache-hit type handling. Modification date: 2026-11-12.
This is not an upstream-endorsed fix or a claim of proven race freedom.

Prysm's pinned root license supplies GNU GPL version-3 terms. The two inspected
source files have no separate per-file copyright/license header. All upstream
rights and applicable notices remain; none are removed or replaced by this
notice. See exact upstream source/license references in [UPSTREAM.json](UPSTREAM.json).
The repository's [LICENSE](../../LICENSE) contains the GPL version-3 text.
No additional upstream source bodies or complete fork are distributed here.

The new patch contributions and `blackbox_test.go.txt` are GPL-3.0-or-later,
consistent with the relay's new-source license. The test source retains its
SPDX notice. This grant for new contributions does not broaden the upstream
license or relicense dependencies. The material is provided without warranty,
under the applicable GPL terms. Preserve these notices with the patch/test
material. Full-fork and binary distribution obligations are not assessed here.

## Hashicorp dependency

The patch uses `github.com/hashicorp/golang-lru` at the already pinned
`v0.5.5-0.20210104140557-80c98217689d` through its public API. No Hashicorp
implementation is copied into this directory or the patch. That dependency
retains MPL-2.0 and its own notices; its exact source/license reference is in
[UPSTREAM.json](UPSTREAM.json). This directory does not redistribute that module.

## Public mathematical fixture

The seed is derived from public BLS12-381 G1 coordinates stated in the fixed
Herumi source cited by [UPSTREAM.json](UPSTREAM.json). Only the coordinate facts
are used; no Herumi implementation or compressed-vector file is copied.
Herumi/native dependencies retain their own licenses. This is not a complete
native notice inventory or a general distribution/compliance clearance.
