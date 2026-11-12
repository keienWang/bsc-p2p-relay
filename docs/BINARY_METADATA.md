# Read-only relay binary metadata

## Purpose and scope

The default CI job runs [collect_binary_metadata.sh](../scripts/collect_binary_metadata.sh)
immediately after its [checked Linux build](LINUX_BUILD.md) and before the existing smoke test. The
collector inspects that same fixed `./bsc-relay` output. It does not build or
execute the relay, call `ldd`, change compilation/linker flags, upload a binary,
or access the network. The surrounding CI still performs its existing build,
tests and smoke test; the collector does not redefine them as read-only.

It prints the checked-out Git commit and tree, binary SHA-256 and byte count,
`file` output, Go build information/build ID, and the `readelf` version, ELF
header, program headers and dynamic entries. The CI run/job and runner image
identify the context. Because the build uses `-buildvcs=false`, the separately
recorded source SHA is CI context, not an embedded or independently attested
source-to-binary relationship.

Required tools are `git`, `sha256sum`, `stat` (GNU-compatible), `file`, `go` and
`readelf`. Missing tools or any failed command fail the step explicitly.
`GOTOOLCHAIN=local` prevents automatic Go toolchain selection/download.
The input must be a regular, non-symlink file before and after collection;
SHA-256, size and file identity/metadata must remain unchanged. These endpoint
checks detect observed drift in a controlled CI job. They are not an atomic
snapshot or protection against a hostile concurrent writer.

The complete metadata appears in the job log, without an uploaded executable.
Review the measured `PT_GNU_STACK`, all `PT_LOAD` flags, ELF type,
`PT_GNU_RELRO`, dynamic `BIND_NOW`/`FLAGS`/`FLAGS_1` and `NEEDED` entries together.
The collector records properties rather than imposing a hardening policy or
classifying linker warnings as vulnerabilities. A command failure or changed
input makes the collection incomplete; partial output is not a successful check.

## Historical observation versus a new measurement

The [targeted Herumi evidence](native-herumi/HERUMI_ARCHIVE_EVIDENCE.json)
records one earlier Go **1.26.10** linux/amd64, CGO-enabled executable with SHA-256
`63db65d60fa5f6728f01854053c056c402ea240b453f16d55538f1e9e8366491`.
Its inspected ELF had `GNU_STACK RW`, no writable/executable LOAD segment,
`ET_EXEC`, `GNU_RELRO`, and no `BIND_NOW`. Those facts apply only to that binary.

The Go **1.26.11** [main run 43226755439](https://github.com/keienWang/bsc-p2p-relay/actions/runs/43226755439)
for commit `fd52cc2ddcea7eb30a4bada576483cb832801690` built the relay and passed
its smoke test, but did not record final ELF metadata or a binary digest.
Its `static_code.o` missing `.note.GNU-stack` warning alone does not establish
the final executable's stack flags. The older result must not be extrapolated.
This collector is a way to obtain a **new, separately identified measurement**;
adding it does not establish a current ELF result until its job succeeds and
the output is reviewed.

Static ELF metadata does not establish whole-process dynamic memory permissions,
runtime JIT absence, vulnerability reachability, complete native source/license
coverage, reproducible compilation, resolution of the known Prysm BLS cache
issue, network interoperability, or production readiness.

## Local contract checks

`python3 -m unittest discover -s scripts -p 'test_binary_metadata.py'` uses
isolated fake tools and temporary files. It needs no Go installation, ELF
execution, compilation or network. `bash -n scripts/collect_binary_metadata.sh`
checks shell syntax. Neither substitutes for hosted inspection of a real build.
