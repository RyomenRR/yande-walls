#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[ubuntu-default-wallpaper] %s\n' "$*"
}

pick_default() {
  local candidates=(
    "/usr/share/backgrounds/ubuntu-default-greyscale-wallpaper.png"
    "/usr/share/backgrounds/ubuntu-wallpaper-d.png"
    "/usr/share/backgrounds/warty-final-ubuntu.png"
  )

  local c
  for c in "${candidates[@]}"; do
    if [ -f "$c" ]; then
      printf '%s\n' "$c"
      return 0
    fi
  done

  return 1
}

set_wallpaper() {
  local image="$1"
  local uri="file://$image"

  if command -v gsettings >/dev/null 2>&1 \
    && gsettings list-schemas | grep -q '^org.gnome.desktop.background$'; then
    gsettings set org.gnome.desktop.background picture-uri "$uri"
    if gsettings writable org.gnome.desktop.background picture-uri-dark >/dev/null 2>&1; then
      gsettings set org.gnome.desktop.background picture-uri-dark "$uri"
    fi
    log "Wallpaper set via gsettings: $image"
    return 0
  fi

  if command -v swaymsg >/dev/null 2>&1 && [ -n "${SWAYSOCK:-}" ]; then
    swaymsg output '*' bg "$image" fill >/dev/null
    log "Wallpaper set via swaymsg: $image"
    return 0
  fi

  if command -v feh >/dev/null 2>&1; then
    feh --bg-fill "$image"
    log "Wallpaper set via feh: $image"
    return 0
  fi

  if command -v xfconf-query >/dev/null 2>&1; then
    xfconf-query -c xfce4-desktop -p /backdrop/screen0/monitor0/image-path -s "$image" >/dev/null 2>&1 || true
    xfconf-query -c xfce4-desktop -p /backdrop/screen0/monitor0/workspace0/last-image -s "$image" >/dev/null 2>&1 || true
    log "Wallpaper set via xfconf-query: $image"
    return 0
  fi

  if command -v nitrogen >/dev/null 2>&1; then
    nitrogen --set-zoom-fill "$image" --save
    log "Wallpaper set via nitrogen: $image"
    return 0
  fi

  return 1
}

main() {
  local image
  # If a yandere wallpaper slideshow/process is running, stop it so the default wallpaper takes effect
  if command -v pkill >/dev/null 2>&1; then
    pkill -f "yande.py" || true
    pkill -f "yandere.sh" || true
    pkill -f "yande.sh" || true
  fi
  # Remove any pid/lock file left by yandere-wallpaper so it doesn't immediately restart
  if [ -n "${XDG_STATE_HOME:-}" ]; then
    rm -f "${XDG_STATE_HOME}/yandere-wallpaper/run.lock" 2>/dev/null || true
  else
    rm -f "$HOME/.local/state/yandere-wallpaper/run.lock" 2>/dev/null || true
  fi

  if ! image="$(pick_default)"; then
    log "Could not find an Ubuntu default wallpaper in /usr/share/backgrounds"
    exit 1
  fi

  if ! set_wallpaper "$image"; then
    log "No supported wallpaper setter found for your desktop environment."
    log "Default wallpaper path: $image"
    exit 1
  fi
}

main "$@"
