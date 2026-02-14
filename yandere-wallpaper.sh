#!/usr/bin/env bash
set -euo pipefail

# Load configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/yandere-wallpaper.conf" ]; then
    source "$SCRIPT_DIR/yandere-wallpaper.conf"
fi

exec python3 "$SCRIPT_DIR/yandere_wallpaper.py" "$@"
