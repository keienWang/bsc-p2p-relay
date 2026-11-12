#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Inspect the default CI build output. Never execute, rebuild or upload it.
set -euo pipefail

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
checked() {
    local status
    "$@" || {
        status=$?
        printf 'ERROR: metadata command failed (exit %s): %s\n' "$status" "$*" >&2
        return "$status"
    }
}
show() { printf '\n== %s ==\n' "$*"; checked "$@"; }

[[ $# -eq 0 ]] || fail 'no arguments accepted; the input is fixed at ./bsc-relay'
cd -- "${BASH_SOURCE[0]%/*}/.."
for tool in git sha256sum stat file go readelf; do
    command -v "$tool" >/dev/null || fail "required tool not found: $tool"
done
# Even outside CI, metadata collection must not select/download a toolchain.
export GOTOOLCHAIN=local
binary=./bsc-relay
regular_input() {
    [[ -f "$binary" && ! -L "$binary" ]] || fail 'input must be a regular, non-symlink ./bsc-relay'
}
snapshot() {
    regular_input
    fingerprint=$(checked stat -c '%d:%i:%f:%s:%y:%z' -- "$binary")
    bytes=$(checked stat -c '%s' -- "$binary")
    digest=$(checked sha256sum -- "$binary")
    digest=${digest%% *}
    [[ "$bytes" =~ ^[0-9]+$ && "$digest" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid size or SHA256 output'
    regular_input
}

snapshot
before_fingerprint=$fingerprint
before_digest=$digest
before_bytes=$bytes
printf 'binary_path=%s\nbinary_bytes_before=%s\nbinary_sha256_before=%s\n' "$binary" "$bytes" "$digest"
show git rev-parse --verify HEAD
show git rev-parse --verify 'HEAD^{tree}'
show file -- "$binary"
show go version -m "$binary"
show go tool buildid "$binary"
show readelf --version
show readelf -hW "$binary"
show readelf -lW "$binary"
show readelf -dW "$binary"
snapshot
printf '\nbinary_bytes_after=%s\nbinary_sha256_after=%s\n' "$bytes" "$digest"
[[ "$digest" == "$before_digest" ]] || fail 'binary SHA256 changed during metadata collection'
[[ "$fingerprint" == "$before_fingerprint" && "$bytes" == "$before_bytes" ]] || fail 'binary identity or metadata changed during collection'
printf 'PASS: metadata collected from unchanged regular input; ELF properties require review, not a vulnerability verdict.\n'
