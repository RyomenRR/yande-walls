# yandere-wallpaper

A small script to fetch anime wallpapers from booru-style sites, optionally compose collages from a stock of portrait images, and set the desktop wallpaper.

## Features
- Fetch images from yande.re, konachan.com and gelbooru.
- Single-instance runs with preemption: starting a new run will terminate an existing run and cleanup partial downloads.
- Downloads are written to temporary `.part` files and renamed atomically on success.
- Collage mode: combine multiple portrait images into a single wallpaper.
- Configurable ratings: enable/disable `safe`, `questionable`, and `explicit` via config or environment.
- When a collage is successfully set the script deletes the source stock images used for that collage (the generated collage image is kept).

## Requirements
- Python 3.8+
- Optional: Pillow (for collage mode). Install with:

```bash
pip install Pillow
```

## Files
- `main.py` — main script.
- `configuration.conf` — optional configuration file (shell-style KEY=VALUE pairs).

## Configuration

Edit `configuration.conf` (or export env variables) to change behavior. Example entries added by default:

```properties
safe=0
questionable=1
explicit=1
```

- These three keys control which ratings are selected. Values are `0` (disabled) or `1` (enabled).
- You may instead set `YANDERE_RATINGS` as a comma-separated override, for example:

```bash
export YANDERE_RATINGS="questionable,explicit"
```

Behavior: if the configuration results in exactly one selected rating the script will prefer a single-image wallpaper; if two or more ratings are selected it will enable collage behavior (combine multiple portrait images). If no ratings are configured the script falls back to the existing `COLLAGE_MODE` value.

Other useful environment variables / keys (also available in the config):
- `COLLAGE_MODE` — enable collage mode (1/0) if no ratings override.
- `STOCK_TARGET` — how many portrait images to keep in the stock cache.
- `DOWNLOAD_THREADS`, `RUN_TIMEOUT`, etc. — see `configuration.conf` for defaults.

## How it handles files
- Downloads are saved first as `.part` files. If a run is interrupted these `.part` files are removed when the next run starts (or when a running process is preempted).
- Previously set wallpapers are preserved in the cache; the script no longer deletes the previous wallpaper file automatically.
- When a collage is successfully created and set, the three source stock images used to build that collage are deleted from the stock folder (so stock is replenished afterwards).

## Running

Make the script executable and run it directly:

```bash
chmod +x main.py
./main.py
```

Or run via the provided shell wrapper (if present):

```bash
bash yandere.sh
```

To test ratings behavior quickly, edit `configuration.conf` and toggle `safe/questionable/explicit`, or run with an env override:

```bash
YANDERE_RATINGS="safe" ./main.py   # single-image (safe only)
YANDERE_RATINGS="questionable,explicit" ./main.py  # collage
```

## Troubleshooting
- If wallpapers are not applying, make sure a supported setter is available (`gsettings`, `feh`, `swaymsg`, `xfconf-query`, or `nitrogen`).
- If collage mode fails, install Pillow.

## Contributing
Patches welcome. Keep changes focused and run the script locally to validate behavior.

## License
Personal / repository default — add a license file if you want to publish.

**Configuration Locations**
- The script looks for `configuration.conf` in these places (in order):
	- path set by `YANDERE_CONFIG` env var
	- current working directory
	- the script directory
	- `~/.config/configuration.conf`

**Ratings Behavior (safe/questionable/explicit)**
- Configure ratings via `configuration.conf` entries or `YANDERE_RATINGS` env var.
- Example config lines:

```properties
safe=0
questionable=1
explicit=1
```

- If exactly one rating is selected the script prefers a single-image wallpaper. If two or more ratings are selected the script enables collage mode. If no ratings are set the `COLLAGE_MODE` setting is used.

**Stock refresh when ratings change**
- The script persists the last-used rating selection in the state directory. If you change ratings, the next run will clear the portrait `stock/` folder and download fresh images matching the new selection.

**Partial downloads and safety**
- Downloads are written to `.part` files and removed on failure or at startup. Starting a new run will attempt to preempt an existing run and clean up leftover `.part` files.

**Debugging**
- The script logs the active `SELECTED_RATINGS` and whether collage is enabled at startup. If you see explicit images despite disabling them, check which config file is being read and the startup log.

