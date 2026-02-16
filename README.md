# yandere-wallpaper

A small script to fetch anime wallpapers from booru-style sites, optionally compose collages from a stock of portrait images, and set the desktop wallpaper.

## Features
- Fetch images from yande.re, konachan.com and gelbooru.
- Single-instance runs with preemption: starting a new run will terminate an existing run and cleanup partial downloads.
- Downloads are written to temporary `.part` files and renamed atomically on success.
- **Three collage modes**:
  - **Mode 0**: Landscape-only stock mode (maintains landscape cache, reuses images across runs)
  - **Mode 1**: Stock-based collages (maintains portrait cache, reuses images across runs)
- **Mode 2**: Alternating collage/landscape mode (keeps portrait + landscape caches and shows one type per run)
- Configurable ratings: enable/disable `safe`, `questionable`, and `explicit` via config or environment.

## Requirements
- Python 3.8+
- Optional: Pillow (for collage mode). Install with:

```bash
pip install Pillow
```

## Files
- `yande.py` — wallpaper setter entrypoint (runs the slideshow / manual change without downloading).
- `downloader.py` — background downloader helper that keeps the portrait/landscape stock filled.
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

Behavior: the `COLLAGE_MODE` setting controls collage behavior:
- **0**: Landscape-only stock mode (maintains landscape cache)
- **1**: Stock-based collages from cached portrait images (keeps cache between runs while automatically picking enough portraits to fill `TARGET_WIDTH` using `MIN_TILE_WIDTH` as the minimum tile width)
- **2**: Alternating collage/landscape selection (reuses portrait and landscape caches sized via `MODE2_PORTRAIT_TARGET` / `MODE2_LANDSCAPE_TARGET`)

The rating selection (`safe`, `questionable`, `explicit`) only determines which images are downloaded, not the collage mode.

- Other useful environment variables / keys (also available in the config):
- `COLLAGE_MODE` — collage behavior: 0=off, 1=stock-based, 2=alternating
- `STOCK_TARGET` — how many portrait images to keep in stock (mode 1 only; Mode 2 uses this value as its portrait cache target)
- `MODE2_PORTRAIT_TARGET`, `MODE2_LANDSCAPE_TARGET` — optional overrides for Mode 2 cache sizes (default portraits = `STOCK_TARGET`, landscapes ≈ `STOCK_TARGET / 3`, at least 1)
- `MIN_TILE_WIDTH` — minimum width per collage tile; the script determines how many tiles fit in `TARGET_WIDTH` (always at least two) and uses that many portraits per collage.
- `NEXT_WALLPAPER_COUNTDOWN_PATH` — optional file path (e.g. `~/.cache/yandere-wallpaper/next-wallpaper-countdown`). When `SLIDESHOW_MINUTES > 0`, the script writes an `MM:SS` countdown between each slide so you can display it in a status bar/polybar block.
- `DOWNLOAD_THREADS`, `RUN_TIMEOUT`, etc. — see `configuration.conf` for defaults.

## How it handles files

**Mode 0 (landscape-only stock):**
- Maintains a stock of landscape images in `~/.cache/yandere-wallpaper/stock-landscape/`
- Downloads new landscapes when stock falls below minimum
- Sets a single landscape image from stock
- Deletes the landscape image after setting (stock replenishes automatically)

**Mode 1 (stock-based collage):**
- Maintains a stock of portrait images in `~/.cache/yandere-wallpaper/stock/`
- Downloads new portraits when stock falls below `STOCK_TARGET`
- Creates collages from enough random stock portraits to fill `TARGET_WIDTH` while respecting `MIN_TILE_WIDTH`
- Deletes the portraits used in each collage (stock replenishes automatically)
- Stores every successful collage (and any fallback landscape downloads) under `~/.cache/yandere-wallpaper/used-walls/` with sequential filenames (`wallpaper-233.jpg`, `wallpaper-234.jpg`, …) so the most recent wallpapers are easy to locate

- **Mode 2 (alternating collage/landscape):**
- Keeps portrait stock in `~/.cache/yandere-wallpaper/stock/` (target derived from `STOCK_TARGET`, unless `MODE2_PORTRAIT_TARGET` overrides it) and landscape stock in `~/.cache/yandere-wallpaper/stock-landscape/` (target defaults to roughly one third of the portrait cache, min 1, unless `MODE2_LANDSCAPE_TARGET` overrides it)
- Alternates between building a collage from 3 portraits and showing a single landscape, deleting the source images after use
- Strict alternation gives you one collage run followed by one landscape run, consuming both caches in lock step
- The script refills whichever cache is low before each type of run so the proportions stay near 30 portraits / 10 landscapes by default
- Logs every Mode 2 run into `~/.local/state/yandere-wallpaper/mode2.log`, recording the action (`collage` or `landscape`) plus running totals so the next run knows which type comes next and you can see how many of each have been shown.

## Running

### Linux/macOS

Make the wallpaper setter executable and run it directly:

```bash
chmod +x yande.py
./yande.py
```

Or run via the provided shell wrapper:

```bash
bash yandere.sh
```

### Background downloader helper

The downloader helper watches the portrait/landscape caches and refills them automatically. Run it in the background (or via a systemd/user service):

```bash
python3 downloader.py &
```

### Testing & Configuration

To test ratings behavior quickly on any platform, edit `configuration.conf` and toggle `safe/questionable/explicit`, or run with an env override:

```bash
# Linux/macOS
YANDERE_RATINGS="safe" ./yande.py   # single-image (safe only)
YANDERE_RATINGS="questionable,explicit" ./yande.py  # collage
```

## Troubleshooting

### Linux/macOS
- If wallpapers are not applying, make sure a supported setter is available (`gsettings`, `feh`, `swaymsg`, `xfconf-query`, or `nitrogen`).
- If collage mode fails, install Pillow.

### Cross-platform
- Check that image files are valid (readable, correct format).
- Ensure download folder has sufficient disk space.
- For display server issues on Linux, verify your desktop environment supports one of the available setters.
- **Mode 2 behavior**: Mode 2 alternates between collages and landscapes, keeping portrait/landscape caches and deleting the source images after each run. Use Mode 0 or Mode 1 if you want to hold onto exported wallpapers.

## Contributing
Patches welcome. Keep changes focused and run the script locally to validate behavior.

## License
Personal / repository default — add a license file if you want to publish.

**Configuration Locations**
- The script looks for `configuration.conf` in these places (in order):
	- path set by `YANDERE_CONFIG` env var
	- current working directory
	- the script directory
	- `~/.config/configuration.conf` (Linux/macOS only)

**Ratings Behavior (safe/questionable/explicit)**
- Configure ratings via `configuration.conf` entries or `YANDERE_RATINGS` env var.
- Rating selection determines which images are downloaded. For example:
  - `safe=1, questionable=0, explicit=0` → only safe images
  - `questionable=1, explicit=1` → questionable and explicit images (safe is excluded)
- The `COLLAGE_MODE` setting independently controls whether collage is enabled (1) or single-image mode (0), regardless of rating selection.

**Stock refresh when ratings change**
- The script persists the last-used rating selection in the state directory. If you change ratings, the next run will clear the portrait `stock/` folder and download fresh images matching the new selection.

**Partial downloads and safety**
- Downloads are written to `.part` files and removed on failure or at startup. Starting a new run will attempt to preempt an existing run and clean up leftover `.part` files.

**Debugging**
- The script logs the active `SELECTED_RATINGS` and whether collage is enabled at startup. If you see explicit images despite disabling them, check which config file is being read and the startup log.
