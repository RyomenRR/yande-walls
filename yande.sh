#!/usr/bin/env bash
set -euo pipefail

# Load configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/configuration.conf" ]; then
    # Export any variables defined in configuration.conf so they reach the Python process
    set -a
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/configuration.conf"
    set +a
fi

exec python3 "$SCRIPT_DIR/yande.py" "$@"
