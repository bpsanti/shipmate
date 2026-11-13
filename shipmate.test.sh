#!/usr/bin/env bash
# Runs the shipmate tests (synthetic fixtures only).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="${PYTHON:-}"
if [ -z "$py" ]; then
  for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$c" >/dev/null 2>&1; then py="$c"; break; fi
  done
fi
exec "$py" "$here/shipmate_test.py" "$@"
