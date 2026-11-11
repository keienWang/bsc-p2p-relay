#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail

# Match worker(t)'s cwd, flags and inherited environment. This separates cold
# build preparation from the suite's unchanged deadline; it is not a runtime
# speedup. The suite still builds and starts its own fresh worker from source.
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../internal/canonicalisolation"
temp_dir=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/canonical-prebuild.XXXXXXXX")
trap 'rm -rf -- "$temp_dir"' EXIT

printf 'canonical prebuild cwd=%s go=%s\n' "$PWD" "$(command -v go)"
go version
go env GOCACHE GOMODCACHE GOFLAGS GOTOOLCHAIN GOWORK CGO_ENABLED CC
printf 'GOMAXPROCS=%s\n' "${GOMAXPROCS-}"
start=$SECONDS
printf 'canonical worker prebuild started (175s TERM + 5s KILL)\n'
status=0
# Do not add the outer suite's ldflags: worker(t) does not pass them.
timeout --signal=TERM --kill-after=5s 175s go build -race -tags=canonicalisolation -o "$temp_dir/worker" ./worker || status=$?
printf 'canonical worker prebuild finished: elapsed_seconds=%d exit=%d\n' "$((SECONDS - start))" "$status"
# Only this script's private output is removed. It is never executed or reused.
exit "$status"
