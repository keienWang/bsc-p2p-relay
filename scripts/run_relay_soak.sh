#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Opt-in only. All relay traffic stays inside one process or on 127.0.0.1.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v flock >/dev/null || { echo 'flock is required to prevent concurrent soak processes' >&2; exit 2; }
exec 9>"${TMPDIR:-/tmp}/bsc-relay-soak.lock"
flock -n 9 || { echo 'Another relay soak is running; not starting competing load' >&2; exit 2; }
output=$(mktemp -d "${TMPDIR:-/tmp}/bsc-relay-soak.XXXXXX")
export GOMAXPROCS="${GOMAXPROCS:-2}" GOTOOLCHAIN=local GOPROXY=off
export RELAY_SOAK=1 RELAY_SOAK_DURATION="${RELAY_SOAK_DURATION:-5s}" RELAY_SOAK_SEED="${RELAY_SOAK_SEED:-20261110}" RELAY_SOAK_REPORT="$output/report.json"
{
 date -u
 go version
 uname -a
 printf 'harness_revision=http-fail-closed-v2\n'
 printf 'GOMAXPROCS=%s duration_per_scenario=%s seed=%s\n' "$GOMAXPROCS" "$RELAY_SOAK_DURATION" "$RELAY_SOAK_SEED"
 git rev-parse HEAD
 git status --short
 sha256sum internal/relay/soak*_test.go scripts/run_relay_soak.sh
 test ! -r /proc/loadavg || cat /proc/loadavg
} > "$output/environment.txt"
args=(-count=1 -timeout="${RELAY_SOAK_TIMEOUT:-3m}" -run='^TestSoak(MsgPipe|Quantiles)$' -v)
if [[ "${RELAY_SOAK_RACE:-0}" == 1 ]]; then args+=(-race); fi
printf 'Report and log directory: %s\n' "$output"
set +e
go test "${args[@]}" ./internal/relay 2>&1 | tee "$output/test.log"
statuses=("${PIPESTATUS[@]}")
set -e
test_status=${statuses[0]}
log_status=${statuses[1]}
status=$test_status
summary_fd=1
if (( log_status != 0 )); then
    if (( status == 0 )); then status=$log_status; fi
    printf 'ERROR: log capture failed (tee exit %s); test exit=%s; log may be incomplete\n' \
        "$log_status" "$test_status" >&2
    summary_fd=2
fi
printf '%s\n' "$status" > "$output/exit-code.txt"
printf 'Exit=%s; artifacts: %s\n' "$status" "$output" >&"$summary_fd"
exit "$status"
