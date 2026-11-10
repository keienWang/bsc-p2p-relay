# Targeted Herumi archive source and notice evidence

Status: engineering evidence limited to the selected linux/amd64 Herumi archive, its recorded source associations and selected notices. Static observations of one QA executable provide context. This is not a complete native inventory, SBOM, license-compliance package, corresponding-source bundle, security audit, reproducible-build attestation or production approval.

No source rebuild or transaction experiment was performed for this evidence. Other CGO/native components, including BLST and secp256k1, and full C/C++ runtime and OS provenance are outside its coverage. The existing [Go SBOM](../SBOM.md) retains its original scope.

See [machine-readable evidence](HERUMI_ARCHIVE_EVIDENCE.json), the original [MCL copyright text](MCL.COPYRIGHT.txt), and the [prepared-only no-transaction plan](NO_TRANSACTION_CONNECTION_PLAN.md). [SHA256SUMS.json](SHA256SUMS.json) records the four files' byte counts and SHA-256 values; it excludes itself.

## Artifact identity

The module is `github.com/herumi/bls-eth-go-binary v1.31.0`, associated with [commit ecb2d6d6d613faa5de749699432721b8342a1798](https://github.com/herumi/bls-eth-go-binary/commit/ecb2d6d6d613faa5de749699432721b8342a1798).

The Go module checksum was independently recomputed as `h1:9eeW3EA4epCb7FIHt2luENpAW69MvKGL5jieHlBiP+w=`. All 50 regular files in the module ZIP match the cached files and the fixed public snapshot's Git blob hashes and sizes. The source contents referenced by submodule Gitlinks are outside the Go module ZIP.

The selected [linux/amd64 archive](https://github.com/herumi/bls-eth-go-binary/blob/ecb2d6d6d613faa5de749699432721b8342a1798/bls/lib/linux/amd64/libbls384_256.a) has:

- Size: **916,592 bytes**.
- Git blob SHA-1: `9168b622e2d6972855ed00b3fce7578fea40cb0c`.
- SHA-256: `498c90b7e3f3cf7a819b2bfd0b619162d8c38e1a29bf8d924821eaba616c9873`.
- Members: `bls_c384_256.o`, `fp.o`, `static_code.o`, `bint64.o`, `base64.o`.

Its local bytes match the archive blob recorded in the public snapshot. This establishes content identity, not the archive's compilation history.

## Recorded source relationships

The parent snapshot records `src/bls` as [herumi/bls@f1cfa24cc64cadf79d7cbcf00a745f7a886f91d9](https://github.com/herumi/bls/commit/f1cfa24cc64cadf79d7cbcf00a745f7a886f91d9). That BLS snapshot records `mcl` as [herumi/mcl@c44f016ce24e31260522f2dca4b424fa5f1ab35f](https://github.com/herumi/mcl/commit/c44f016ce24e31260522f2dca4b424fa5f1ab35f). The repository URLs agree with the recorded `.gitmodules` files.

The following additional source relationships were checked against full file bytes and their recorded Git blob hashes:

1. [BLS `src/bls_c384_256.cpp`](https://github.com/herumi/bls/blob/f1cfa24cc64cadf79d7cbcf00a745f7a886f91d9/src/bls_c384_256.cpp) is an 85-byte translation unit. It defines `MCLBN_FP_UNIT_SIZE=6` and `MCLBN_FR_UNIT_SIZE=4`, then includes `bls_c_impl.hpp`.
2. [BLS `src/bls_c_impl.hpp`](https://github.com/herumi/bls/blob/f1cfa24cc64cadf79d7cbcf00a745f7a886f91d9/src/bls_c_impl.hpp) is 28,850 bytes, blob `0da666dfbeda933dcdfbe7158fcf61bf925ccffa`, SHA-256 `25787aaa5c2260d695809975e224732a6980bf3a74037c856924888be8f075bc`. It directly includes `bls/bls.h`, `mcl/impl/bn_c_impl.hpp`, `cybozu/sha2.hpp` and `cybozu/endian.hpp`, plus conditional `thread` and `qcoeff-bn254.hpp` includes. This inspection checked identity, notices and include relationships, not cryptographic correctness.
3. [BLS `include/bls/bls.hpp`](https://github.com/herumi/bls/blob/f1cfa24cc64cadf79d7cbcf00a745f7a886f91d9/include/bls/bls.hpp) is a separate C++ wrapper. Its BSD declaration must not substitute for inspection of the C implementation above.
4. [MCL `src/fp.cpp`](https://github.com/herumi/mcl/blob/c44f016ce24e31260522f2dca4b424fa5f1ab35f/src/fp.cpp) directly includes `cybozu/sha2.hpp`. Its static/x64 helper section includes `xbyak/xbyak_util.h` and defines `Xbyak::util::Cpu g_cpu`. When `MCL_USE_XBYAK` is not defined, it sets `XBYAK_ONLY_CLASS_CPU`. Separate conditions include `fp_static_code.hpp` and `fp_generator.hpp`.
5. [Vendored `src/xbyak/xbyak_util.h`](https://github.com/herumi/mcl/blob/c44f016ce24e31260522f2dca4b424fa5f1ab35f/src/xbyak/xbyak_util.h) does not include the JIT assembler header `xbyak.h` in its CPU-only branch. The alternative branch includes it.

These relationships agree with the recorded static-code recipe and the inspected executable's retained Xbyak CPU-helper and cybozu SHA symbols. They do not prove every historical build flag, every preprocessor result or whole-process runtime JIT absence. Transitive implementation bodies, including `mcl/impl/bn_c_impl.hpp`, were not recursively reviewed.

## License evidence and its limits

The accompanying JSON records evidence levels separately: a full copyright/conditions/disclaimer text, a repository declaration, a file-header declaration, or no file-local notice observed. None is a blanket compliance verdict.

- **MCL:** its pinned [COPYRIGHT](https://github.com/herumi/mcl/blob/c44f016ce24e31260522f2dca4b424fa5f1ab35f/COPYRIGHT) contains `Copyright (c) 2015 MITSUNARI Shigeo`, the three BSD redistribution/nonendorsement conditions and the warranty disclaimer. The complete original bytes are included as [MCL.COPYRIGHT.txt](MCL.COPYRIGHT.txt).
- **BLS:** the pinned [README](https://github.com/herumi/bls/blob/f1cfa24cc64cadf79d7cbcf00a745f7a886f91d9/readme.md) declares modified new BSD / BSD-3-Clause. The inspected C++ wrapper has a matching header declaration. The actual direct C interface include, [bls.h](https://github.com/herumi/bls/blob/f1cfa24cc64cadf79d7cbcf00a745f7a886f91d9/include/bls/bls.h), has the same blob as the shipped Go-module header and an explicit BSD declaration.
- **Herumi wrapper distribution:** shipped C headers declare modified BSD / BSD-3-Clause. The nontruncated parent snapshot has no standalone LICENSE/COPYRIGHT file. The BLS snapshot likewise has no standalone file with those names. This is not by itself a finding of noncompliance.
- **Vendored Xbyak and cybozu:** the pinned [Xbyak main header](https://github.com/herumi/mcl/blob/c44f016ce24e31260522f2dca4b424fa5f1ab35f/src/xbyak/xbyak.h) and [cybozu SHA2 header](https://github.com/herumi/mcl/blob/c44f016ce24e31260522f2dca4b424fa5f1ab35f/include/cybozu/sha2.hpp) explicitly declare BSD-3-Clause. Their identities are the recorded vendored file blobs. Independent upstream commits were not established.
- **Files without their own observed notice:** `bls_c384_256.cpp`, `bls_c_impl.hpp`, `fp.cpp` and `xbyak_util.h` contain no file-local copyright/license text. Repository-level notices and declarations remain relevant evidence; this appendix does not assign legal coverage to every file merely by proximity.
- **Lifted-ElGamal:** MCL's README lists it among included BSD-3-Clause software. Its inclusion in this archive or executable was not established.

A complete distribution review still needs file-level inclusion and attribution analysis for the selected native build and other dependencies. The MCL notice is not silently extended to every component or author.

## Recipe and executable observations

The recorded parent configuration sets `ETH_CFLAGS=-DBLS_ETH`. The pinned [BLS Makefile.onelib](https://github.com/herumi/bls/blob/f1cfa24cc64cadf79d7cbcf00a745f7a886f91d9/Makefile.onelib) selects `MCL_STATIC_CODE` and `MCL_DONT_USE_XBYAK` for its x86-64 ETH case. The [MCL Makefile](https://github.com/herumi/mcl/blob/c44f016ce24e31260522f2dca4b424fa5f1ab35f/Makefile) builds a dump generator, uses it to produce `static_code.asm`, and assembles that output. Build-time generation is distinct from runtime JIT use.

The checked-in release recipe does not pin a complete compiler/assembler/archiver/runner image. The `bls_c384_256.o` and `fp.o` members contain the descriptive compiler string `Ubuntu clang version 14.0.0-1ubuntu1`. That string is not a compiler binary hash or complete build attestation.

The inspected QA executable, SHA-256 `63db65d60fa5f6728f01854053c056c402ea240b453f16d55538f1e9e8366491`, is linux/amd64 with CGO enabled. Its ELF declares a read/write, non-executable GNU_STACK and no writable/executable LOAD segment. It is ET_EXEC, has GNU_RELRO, and has no BIND_NOW flag. These are observed properties, not a vulnerability verdict or a reason to change flags without a separate engineering case. No process memory-map observation was performed.

## What remains unproven

- That these recorded source revisions produced the archive, or that a controlled rebuild reproduces it.
- Complete native build inputs, file-level notices, the transitive corresponding-source closure, and cryptographic/security review. Other CGO/native components remain outside this targeted appendix.
- Runtime memory permissions, safety of future concurrent BLS verification, or resolution of the known Prysm cache race.
- Current-network handshake/retention, target-hardware soak and capacity, authenticated chain head/finality, or production readiness.
