#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
cd -- "${BASH_SOURCE[0]%/*}/.."
exec python3 scripts/noexecstack_candidate.py "$@"
