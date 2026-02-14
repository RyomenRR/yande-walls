#!/usr/bin/env bash
set -euo pipefail

# Load configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/configuration.conf" ]; then
    source "$SCRIPT_DIR/configuration.conf"
fi

exec python3 "$SCRIPT_DIR/main.py" "$@"
