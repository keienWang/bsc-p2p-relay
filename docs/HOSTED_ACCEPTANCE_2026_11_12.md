# Hosted acceptance: Linux build and synthetic scanner controls

This record describes an observed run, not a release or production approval.
Later documentation-only changes retain the implementation below; later code or
configuration changes require their own CI acceptance.

- Public source: [`c27438bf46969dd224db5cbaa49f65b747537787`](https://github.com/keienWang/bsc-p2p-relay/commit/c27438bf46969dd224db5cbaa49f65b747537787).
- Git tree: `9b180c3bd1888330a08b99335a774c7fa591d68a` (184 tracked files).
- [Run 43256861979](https://github.com/keienWang/bsc-p2p-relay/actions/runs/43256861979): **five jobs, 68 steps successful**, using Go 1.26.11.
- Local integration checks had passed 141 Python tests, both documentation
  checkers, all five shell syntax checks and the scoped Git whitespace check.
  These local results were not substituted for hosted Go/native execution.

## Checked Linux delivery artifact

[Default job 131317312179](https://github.com/keienWang/bsc-p2p-relay/actions/runs/43256861979/job/131317312179)
ran the entire default race suite with the qualified linker policy, used the
same helper as the recommended Linux Makefile entry, atomically installed its
checked output, and completed the existing metadata collector and real loopback
smoke. The installation and subsequent collector identified the same artifact:

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| Delivery relay | 50,729,184 | `a40432347222aec447eab0a8421f398a9cf4f11fbc8ee6f74c9def91730013e0` |
| Official tagged race test binary | 110,427,048 | `eb1e8df6cc977ec31cdf59aca7daea3ca183dc1287082767545facb92719c225` |

Both inspected artifacts had one **GNU_STACK RW**, and LOAD permissions
R / RE / R / RW, with no writable/executable LOAD segment. The policy was
`-extldflags=-Wl,-z,noexecstack`; the delivery build retained `-trimpath` and
`-buildvcs=false`. Go deliberately omits linker flags from trimpath buildinfo,
so the receipt uses the exact build command and independently inspected ELF,
not an invented embedded flag field. Source checkout status is context, not a
cryptographic attestation of every compiled byte.

This establishes the checked entry on the recorded Linux/amd64 CGO configuration.
Bare `go build`, other targets, all native CPU paths, process memory mappings,
full hardening and the archive's missing assembly annotation are not qualified
by this observation. See [the build contract](LINUX_BUILD.md).

## Entire official txpool test package

[Official job 131317312279](https://github.com/keienWang/bsc-p2p-relay/actions/runs/43256861979/job/131317312279)
compiled the actual alternate-modfile/tagged relay package with the same linker
policy, inspected that exact test ELF before execution, and ran the whole
binary from `internal/relay` **without a test-name filter**. The official loopback
case emitted RUN then PASS (35.15 seconds). The package exited successfully;
its artifact hash remained unchanged afterward.

The log contains 463 PASS events including subtests and three intentional skips:
`TestSoakMsgPipe`, `TestUniqueCapacityDefaultTTL`, and
`TestUniqueCapacityFailureReport`. Those opt-in workloads were not executed and
are not included in this acceptance. The test uses loopback, not public peers,
real accounts or funded transactions.

## Strict synthetic scanner comparison

[Scanner job 131317312069](https://github.com/keienWang/bsc-p2p-relay/actions/runs/43256861979/job/131317312069)
first passed the existing 11 selected top-level synthetic tests and seven
subtests. It then compiled both comparison binaries successfully from official
`golang/vuln@e672cbe8fbc8c76e28fcb6db0efd9ce354e0dac5` and the experimental
patched source, with byte-identical comparison tests.

| Control | Official baseline | Patched source |
| --- | --- | --- |
| Cancellation error propagation | exact expected assertion, FAIL | PASS |
| Ordinary source trace | PASS | PASS |
| Test-only source trace | exact expected assertion, FAIL | PASS |
| Trace parent / process exit | FAIL / 1 | PASS / 0 |

These are two reproduced synthetic defects, not two discovered production
vulnerabilities. The final receipt verified both classifications, source/test
identities, unchanged post-execution binary hashes, and a non-root namespace
with zero capabilities, no routes and down-only loopback. No real vulnerability
database was queried. The production scanner entry remains unchanged, and its
known full-test-graph coverage limitation is **not** closed by these controls.

Public output is `allowlisted_semantics_not_raw_test2json`: fixed-name events,
assertion codes, diagnostic counts, validated status and identities, rather than
raw environment-bearing logs. Its canonical JSON payload independently verified
as **6,815 bytes**, SHA-256
`187639ece1c3b2f7fc1e6d5cd94b104dfaf17ebcc84ce409958c00621844c0a9`.
Reproduction uses Python `json.dumps(payload, sort_keys=True,
separators=(",", ":"), ensure_ascii=True).encode()` before hashing. Local raw-log
hashes are explicitly a different representation. No masked value was recovered
or encoded around masking. Both final outer success flags were true.

## Other jobs, evidence and limits

The separate noexecstack research job and canonical-isolation job also passed.
Canonical's independently compiled child does **not** inherit the new linker
policy and is not part of the same-policy native acceptance. The three existing
scanner invocations reported no findings within their existing scope, not proof
of complete test-graph or all-dependency coverage.

Selected complete GitHub-visible decoded log digests:

| Job | UTF-8 bytes | SHA-256 |
| --- | ---: | --- |
| Default | 89,326 | `47825111caccf59754bdbdb1b96055f5d3a301ced02a7db90e7f6c7785928914` |
| Official | 630,469 | `2b1b65abd1ee07e15e76b27506cb694fbd7fa15e8a7a96e96e14a73c9a68cac4` |
| Scanner | 164,974 | `cb07a38d7ecc11fbdfeb18df2355efdcbc726ce0865f699b66c9f40fbb713536` |

These hashes identify the captured platform-visible logs, not hidden pre-masking
content. The earlier formatting, trimpath-metadata and verbose-classification
failures remain recorded as failures in the linked research documents.

Public-network stability/long-soak, authenticated modern head and confirmation
support, the Prysm cache race, complete native/source licensing and release
supply-chain qualification remain open. No production release, live transaction
or server deployment was performed by this acceptance run.
