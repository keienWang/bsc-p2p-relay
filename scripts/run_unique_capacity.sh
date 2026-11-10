#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Only loopback HTTP / in-process MsgPipe. No public networking or transactions.
set -euo pipefail
cd "$(dirname "$0")/.."
mode=${1:---short}
case "$mode" in
  --short) run='^TestUnique(Capacity|HTTP.*|CountAudit.*|TransportAudit.*)$'; timeout=60s; unset RELAY_UNIQUE_DEFAULTS RELAY_UNIQUE_FAILURE_PROBE ;;
  --default-ttl) run='^TestUniqueCapacityDefaultTTL$'; timeout=180s; export RELAY_UNIQUE_DEFAULTS=1; unset RELAY_UNIQUE_FAILURE_PROBE ;;
  --failure-probe) run='^TestUniqueCapacityFailureReport$'; timeout=30s; export RELAY_UNIQUE_FAILURE_PROBE=1; unset RELAY_UNIQUE_DEFAULTS ;;
  *) echo 'Usage: run_unique_capacity.sh [--short|--default-ttl|--failure-probe]' >&2; exit 2 ;;
esac
command -v flock >/dev/null || { echo 'flock is required' >&2; exit 2; }
# Share the existing soak lock: do not compete with the old occupancy workload.
exec 9>"${TMPDIR:-/tmp}/bsc-relay-soak.lock"
flock -n 9 || { echo 'Another relay load experiment is running; not starting competing load' >&2; exit 2; }
output=$(mktemp -d "${TMPDIR:-/tmp}/bsc-relay-unique.XXXXXX")
export GOMAXPROCS=2 GOTOOLCHAIN=local GOPROXY=off
export RELAY_UNIQUE_REPORT="$output/report.json"
{
  date -u
  go version
  uname -a
  printf 'harness_revision=unique-capacity-v1\nmode=%s\nGOMAXPROCS=2\n' "$mode"
  git rev-parse HEAD
  git status --short
  sha256sum internal/relay/unique_capacity*_test.go internal/relay/soak*_test.go scripts/run_unique_capacity.sh
  test ! -r /proc/loadavg || cat /proc/loadavg
} > "$output/environment.txt"
printf 'Report and full log: %s\n' "$output"
args=(-p=1 -count=1 -timeout="$timeout" -run="$run" -v)
if [[ "${RELAY_UNIQUE_RACE:-0}" == 1 ]]; then args+=(-race); fi
set +e
go test "${args[@]}" ./internal/relay 2>&1 | tee "$output/test.log"
status=${PIPESTATUS[0]}
set -e
printf '%s\n' "$status" > "$output/exit-code.txt"
printf 'Exit=%s; artifacts: %s\n' "$status" "$output"
exit "$status"
