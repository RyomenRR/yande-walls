#!/usr/bin/env python3
import atexit
import json
import os
import random
import signal
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from threading import Event, Lock, Thread
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path
from dataclasses import dataclass
from queue import Empty, Queue

try:
    import fcntl
except Exception:
    fcntl = None

SCRIPT_DIR = Path(__file__).resolve().parent
DOWNLOADER_SCRIPT = SCRIPT_DIR / "downloader.py"
ALLOW_DOWNLOADS = False


def set_allow_downloads(value: bool) -> None:
    global ALLOW_DOWNLOADS
    ALLOW_DOWNLOADS = value


IS_WINDOWS = sys.platform.startswith("win")

try:
    from PIL import Image, ImageOps
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False


def log(msg: str) -> None:
    print(f"[yande-walls] {msg}")


def countdown_worker(deadline: float, stop_event: Event) -> None:
    if not sys.stderr.isatty():
        return
    while not stop_event.wait(1):
        left = max(0, int(deadline - time.monotonic() + 0.999))
        sys.stderr.write(f"\r[yande-walls] Time left: {left:02d}s ")
        sys.stderr.flush()
        if left <= 0:
            break
    sys.stderr.write("\n")
    sys.stderr.flush()


# Optional path that receives the slideshow countdown for external bars (e.g. polybar, waybar)
_next_wallpaper_timer = os.environ.get("NEXT_WALLPAPER_COUNTDOWN_PATH", "").strip()
NEXT_WALLPAPER_COUNTDOWN_PATH = (
    Path(os.path.expanduser(_next_wallpaper_timer)) if _next_wallpaper_timer else None
)


def format_timer(seconds: int) -> str:
    seconds = max(0, seconds)
    minutes, secs = divmod(seconds, 60)
    return f"{minutes:02d}:{secs:02d}"


def write_next_wallpaper_timer(seconds: int) -> None:
    if NEXT_WALLPAPER_COUNTDOWN_PATH is None:
        return
    try:
        NEXT_WALLPAPER_COUNTDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        NEXT_WALLPAPER_COUNTDOWN_PATH.write_text(format_timer(seconds), encoding="utf-8")
    except Exception:
        pass


def reset_next_wallpaper_timer() -> None:
    write_next_wallpaper_timer(0)


def format_bytes(size: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:5.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TiB"


class DownloadProgress:
    BAR_WIDTH = 24

    def __init__(self, label: str):
        self.label = label
        self.total: int | None = None
        self.downloaded = 0
        self.lock = Lock()
        self._last_line_len = 0
        self.enabled = sys.stderr.isatty()

    def start(self, total: int | None):
        self.total = total
        self._render()

    def update(self, chunk: int):
        if not self.enabled:
            return
        self.downloaded += chunk
        self._render()

    def finish(self):
        if not self.enabled:
            return
        self._render(final=True)

    def _render(self, final: bool = False):
        if not self.enabled:
            return
        downloaded = self.downloaded
        total = self.total
        if total and total > 0:
            frac = min(1.0, downloaded / total)
            filled = int(frac * self.BAR_WIDTH)
            bar = "#" * filled + "-" * (self.BAR_WIDTH - filled)
            status = f"{frac * 100:5.1f}% {format_bytes(downloaded)}/{format_bytes(total)}"
        else:
            filled = min(self.BAR_WIDTH, int((downloaded / 1024) % (self.BAR_WIDTH + 1)))
            bar = "#" * filled + "-" * (self.BAR_WIDTH - filled)
            status = f"{format_bytes(downloaded)}"
        line = f"\r[{self.label}] {bar} {status}"
        pad = max(0, self._last_line_len - len(line))
        with self.lock:
            sys.stderr.write(line)
            if pad:
                sys.stderr.write(" " * pad)
            if final:
                sys.stderr.write("\n")
            sys.stderr.flush()
            self._last_line_len = len(line)


@dataclass
class DownloadTask:
    orientation: str
    label: str


def slideshow_countdown_worker(interval: int, stop_event: Event) -> None:
    if interval <= 0:
        return
    deadline = time.monotonic() + interval
    write_next_wallpaper_timer(interval)
    while not stop_event.wait(1):
        remaining = max(0, int(deadline - time.monotonic() + 0.999))
        write_next_wallpaper_timer(remaining)
        if remaining <= 0:
            break
    write_next_wallpaper_timer(0)


def start_slideshow_timer(interval: int):
    if NEXT_WALLPAPER_COUNTDOWN_PATH is None or interval <= 0:
        return None, None
    stop_event = Event()
    thread = Thread(target=slideshow_countdown_worker, args=(interval, stop_event), daemon=True)
    thread.start()
    return stop_event, thread


def stop_slideshow_timer(stop_event, thread) -> None:
    if stop_event:
        stop_event.set()
    if thread:
        thread.join(timeout=0.1)


if NEXT_WALLPAPER_COUNTDOWN_PATH is not None:
    reset_next_wallpaper_timer()


def read_wallpaper_counter() -> int:
    try:
        return int(COUNTER_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 232


def number_wallpaper(path: Path) -> Path:
    last = read_wallpaper_counter()
    next_number = last + 1 if last >= 233 else 233
    target = USED_WALLPAPER_DIR / f"wallpaper-{next_number}{path.suffix or '.jpg'}"

    try:
        shutil.copy2(path, target)
        try:
            path.unlink()
        except Exception:
            pass
    except Exception:
        try:
            path.rename(target)
        except Exception:
            return path

    COUNTER_FILE.write_text(str(next_number), encoding="utf-8")
    return target


def read_mode2_log():
    default = {"action": "landscape", "total_landscapes": 0, "total_collages": 0}
    if not MODE2_LOG_FILE.exists():
        return default
    last_line = None
    try:
        with MODE2_LOG_FILE.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last_line = line
        if last_line:
            entry = json.loads(last_line)
            return {
                "action": entry.get("action", default["action"]),
                "total_landscapes": int(entry.get("total_landscapes", default["total_landscapes"])),
                "total_collages": int(entry.get("total_collages", default["total_collages"])),
            }
    except Exception:
        return default
    return default


def write_mode2_log(action: str):
    state = read_mode2_log()
    totals = {
        "total_landscapes": state["total_landscapes"] + (1 if action == "landscape" else 0),
        "total_collages": state["total_collages"] + (1 if action == "collage" else 0),
    }
    entry = {
        "timestamp": int(time.time()),
        "action": action,
        "total_landscapes": totals["total_landscapes"],
        "total_collages": totals["total_collages"],
    }
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with MODE2_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "yandere-wallpaper"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "yandere-wallpaper"
STOCK_DIR = CACHE_DIR / "stock"
LANDSCAPE_STOCK_DIR = CACHE_DIR / "stock-landscape"  # For Mode 2
USED_WALLPAPER_DIR = CACHE_DIR / "used-walls"
USED_WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
COUNTER_FILE = STATE_DIR / "wallpaper-counter"
MODE2_LOG_FILE = STATE_DIR / "mode2.log"
STATE_FILE = STATE_DIR / "current_wallpaper"
HELPER_PID_FILE = STATE_DIR / "download-helper.pid"
MIN_WIDTH = int(os.environ.get("MIN_WIDTH", "1600"))
MIN_HEIGHT = int(os.environ.get("MIN_HEIGHT", "900"))
MAX_API_PAGE = int(os.environ.get("MAX_API_PAGE", "300"))
TARGET_WIDTH = int(os.environ.get("TARGET_WIDTH", "1920"))
TARGET_HEIGHT = int(os.environ.get("TARGET_HEIGHT", "1080"))
COLLAGE_MODE = int(os.environ.get("COLLAGE_MODE", "1"))  # 0=off, 1=stock-based, 2=random
STOCK_TARGET = max(3, int(os.environ.get("STOCK_TARGET", "30")))
DOWNLOAD_THREADS = max(2, int(os.environ.get("DOWNLOAD_THREADS", "8")))
SLIDESHOW_MINUTES = max(0, int(os.environ.get("SLIDESHOW_MINUTES", "0")))  # 0=off; >0 minutes between wallpaper changes
RUN_TIMEOUT = max(10, int(os.environ.get("RUN_TIMEOUT", "300")))
LOCK_FILE = STATE_DIR / "run.lock"
SHOW_COUNTDOWN = os.environ.get("SHOW_COUNTDOWN", "0") in {"1", "true", "True"}
SHOW_DOWNLOAD_PROGRESS = os.environ.get("SHOW_DOWNLOAD_PROGRESS", "1") not in {"0", "false", "False", "no", "off"}
MIN_TILE_WIDTH = int(os.environ.get("MIN_TILE_WIDTH", "500"))
DOWNLOAD_HELPER_INTERVAL = max(5, int(os.environ.get("DOWNLOAD_HELPER_INTERVAL", "30")))
_threshold = 0.5
try:
    _threshold = float(os.environ.get("DOWNLOAD_HELPER_THRESHOLD", "0.5"))
except Exception:
    pass
DOWNLOAD_HELPER_THRESHOLD = min(max(_threshold, 0.1), 0.9)


def helper_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def read_helper_pid() -> int | None:
    try:
        if HELPER_PID_FILE.exists():
            return int(HELPER_PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        pass
    return None


def write_helper_pid(pid: int) -> None:
    try:
        HELPER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        HELPER_PID_FILE.write_text(str(pid), encoding="utf-8")
    except Exception:
        pass


def clear_helper_pid() -> None:
    try:
        if HELPER_PID_FILE.exists():
            HELPER_PID_FILE.unlink()
    except Exception:
        pass


def helper_is_running() -> bool:
    pid = read_helper_pid()
    if pid is None:
        return False
    return helper_pid_alive(pid)


def ensure_helper_process():
    if helper_is_running():
        return
    if not DOWNLOADER_SCRIPT.exists():
        return
    cmd = [sys.executable, str(DOWNLOADER_SCRIPT)]
    try:
        with open(os.devnull, "wb") as devnull:
            subprocess.Popen(cmd, stdout=devnull, stderr=devnull, start_new_session=True)
        log("Started download helper in background")
    except Exception as exc:
        log(f"Failed to start download helper: {exc}")


def _target_threshold(target: int) -> int:
    threshold_count = int(target * DOWNLOAD_HELPER_THRESHOLD)
    return max(1, threshold_count)


pending_counts = {"portrait": 0, "landscape": 0}
label_counters = {"portrait": 0, "landscape": 0}
pending_lock = Lock()


def increment_pending(orientation: str, count: int):
    with pending_lock:
        pending_counts[orientation] += count


def decrement_pending(orientation: str):
    with pending_lock:
        pending_counts[orientation] = max(0, pending_counts[orientation] - 1)


def next_label_number(orientation: str) -> int:
    with pending_lock:
        label_counters[orientation] += 1
        return label_counters[orientation]


def get_pending(orientation: str) -> int:
    with pending_lock:
        return pending_counts[orientation]


def current_stock_count(orientation: str) -> int:
    if orientation == "portrait":
        return len(list_stock_images())
    return len(list_landscape_stock())


def target_for_orientation(orientation: str) -> int:
    if EFFECTIVE_COLLAGE_MODE == 2:
        portrait_target, landscape_target = get_mode2_targets()
        return portrait_target if orientation == "portrait" else landscape_target
    if EFFECTIVE_COLLAGE_MODE == 1 and orientation == "portrait":
        return STOCK_TARGET
    if EFFECTIVE_COLLAGE_MODE == 0 and orientation == "landscape":
        return STOCK_TARGET
    return 0


def queue_downloads(task_queue: Queue, orientation: str, count: int):
    if count <= 0:
        return
    tasks = []
    for _ in range(count):
        label = f"{orientation} {next_label_number(orientation)}"
        tasks.append(DownloadTask(orientation=orientation, label=label))
    increment_pending(orientation, len(tasks))
    for task in tasks:
        task_queue.put(task)
    log(f"Download helper: queued {len(tasks)} downloads for {orientation}.")


def enqueue_needed_downloads(task_queue: Queue) -> bool:
    added = False
    mode = EFFECTIVE_COLLAGE_MODE
    if mode == 2:
        for orientation in ("portrait", "landscape"):
            added |= enqueue_for_orientation(task_queue, orientation)
    elif mode == 1:
        added |= enqueue_for_orientation(task_queue, "portrait")
    else:
        added |= enqueue_for_orientation(task_queue, "landscape")
    return added


def enqueue_for_orientation(task_queue: Queue, orientation: str) -> bool:
    target = target_for_orientation(orientation)
    if target <= 0:
        return False
    current = current_stock_count(orientation)
    pending = get_pending(orientation)
    needed = target - current - pending
    if needed <= 0:
        return False
    queue_downloads(task_queue, orientation, needed)
    return True


def download_task_worker(task_queue: Queue, stop_event: Event) -> None:
    while not stop_event.is_set():
        try:
            task = task_queue.get(timeout=0.5)
        except Empty:
            continue
        if task is None:
            task_queue.task_done()
            break
        try:
            deadline = time.monotonic() + RUN_TIMEOUT
            target = target_for_orientation(task.orientation)
            current = current_stock_count(task.orientation)
            if current >= target:
                log(f"Download helper: {task.orientation} stock already at target ({current}/{target}), skipping task.")
                continue
            posts = pick_images(1, task.orientation, deadline)
            if not posts:
                log(f"Download helper: no {task.orientation} images found.")
                continue
            ext, url, _source_name, _rating = posts[0]
            out_dir = STOCK_DIR if task.orientation == "portrait" else LANDSCAPE_STOCK_DIR
            download_file(
                url,
                ext,
                deadline,
                out_dir,
                progress_label=progress_label_for(task.label),
            )
        except Exception as exc:
            log(f"Download helper task failed: {exc}")
        finally:
            decrement_pending(task.orientation)
            task_queue.task_done()


def download_helper_main() -> int:
    set_allow_downloads(True)
    pid = os.getpid()
    write_helper_pid(pid)
    atexit.register(clear_helper_pid)
    task_queue: Queue[DownloadTask | None] = Queue()
    stop_event = Event()
    workers = []
    for _ in range(DOWNLOAD_THREADS):
        worker = Thread(target=download_task_worker, args=(task_queue, stop_event), daemon=True)
        worker.start()
        workers.append(worker)

    log("Download helper is running.")
    try:
        while True:
            added = enqueue_needed_downloads(task_queue)
            if not added:
                time.sleep(DOWNLOAD_HELPER_INTERVAL)
    except KeyboardInterrupt:
        log("Download helper interrupted by user.")
    finally:
        stop_event.set()
        for _ in workers:
            task_queue.put(None)
        for worker in workers:
            worker.join(timeout=1.0)
    return 0
# Format progress label only when download progress is enabled so we don't instantiate DownloadProgress unnecessarily.
def progress_label_for(label: str | None) -> str | None:
    if SHOW_DOWNLOAD_PROGRESS and label:
        return label
    return None
# Mode 2 specific targets (can be overridden via env)
MODE2_PORTRAIT_TARGET_ENV = os.environ.get("MODE2_PORTRAIT_TARGET")
MODE2_LANDSCAPE_TARGET_ENV = os.environ.get("MODE2_LANDSCAPE_TARGET")


def get_mode2_targets() -> tuple[int, int]:
    portrait = STOCK_TARGET
    if MODE2_PORTRAIT_TARGET_ENV:
        try:
            portrait = max(1, int(MODE2_PORTRAIT_TARGET_ENV))
        except Exception:
            portrait = max(1, portrait)
    if portrait < 1:
        portrait = 1

    if MODE2_LANDSCAPE_TARGET_ENV:
        try:
            landscape = max(1, int(MODE2_LANDSCAPE_TARGET_ENV))
        except Exception:
            landscape = max(1, portrait // 3)
    else:
        landscape = max(1, portrait // 3)

    return portrait, landscape
BOORU_SOURCES = [
    {"name": "yande.re", "kind": "moebooru", "base": "https://yande.re"},
    {"name": "konachan.com", "kind": "moebooru", "base": "https://konachan.com"},
]

# Use moebooru-style rating tags for both yande.re and konachan
RATING_TAGS = {
    "moebooru": ["rating:q", "rating:e"],  # questionable + explicit
}

# Mapping of logical rating names to site-specific tag strings
RATING_TAGS_MAP = {
    "moebooru": {
        "safe": "rating:s",
        "questionable": "rating:q",
        "explicit": "rating:e",
    },
}

# HTTP session support with cookies and common headers to reduce Cloudflare triggers
COOKIE_JAR = http.cookiejar.CookieJar()
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# Alternate header variants to try when Konachan/Cloudflare blocks requests
HEADER_VARIANTS = [
    DEFAULT_HEADERS,
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
        "Connection": "keep-alive",
    },
]


def make_opener():
    handlers = []
    cj = COOKIE_JAR
    handlers.append(urllib.request.HTTPCookieProcessor(cj))
    try:
        handlers.append(urllib.request.HTTPSHandler())
    except Exception:
        pass
    return urllib.request.build_opener(*handlers)


GLOBAL_OPENER = make_opener()


def is_cloudflare_response(content_bytes: bytes) -> bool:
    # Look for common Cloudflare/anti-bot strings in HTML responses
    try:
        lower = content_bytes.lower()
        markers = [b"attention required", b"just a moment", b"cf-challenge", b"cf-","captcha", b"checking your browser"]
        for m in markers:
            if m in lower:
                return True
    except Exception:
        pass
    return False


def warm_site(base_url: str, deadline: float) -> bool:
    """Hit the site root to obtain cookies and try to warm a session.

    Returns True if the site responded without obvious Cloudflare blocks.
    """
    try:
        req = urllib.request.Request(base_url, headers=DEFAULT_HEADERS)
        with GLOBAL_OPENER.open(req, timeout=timeout_for(deadline, 6.0)) as resp:
            raw = resp.read()
            if is_cloudflare_response(raw):
                return False
            return True
    except Exception:
        return False

# Config file (optional). If present, keys: safe,questionable,explicit with 0/1 values.
# The loader will look in several locations (env override, cwd, script dir, home .config).
CONFIG_ENV_PATH = os.environ.get("YANDERE_CONFIG")
CONFIG_CANDIDATES = [
    Path(os.getcwd()) / "configuration.conf",
    Path(__file__).resolve().parent / "configuration.conf",
    Path.home() / ".config" / "configuration.conf",
]
if CONFIG_ENV_PATH:
    try:
        CONFIG_CANDIDATES.insert(0, Path(CONFIG_ENV_PATH))
    except Exception:
        pass

# File to persist last-used rating selection
RATINGS_STATE_FILE = STATE_DIR / "selected_ratings"


def load_saved_ratings():
    try:
        txt = RATINGS_STATE_FILE.read_text(encoding="utf-8").strip()
        if not txt:
            return []
        return [p for p in txt.split(",") if p]
    except Exception:
        return []


def save_selected_ratings(sel):
    try:
        RATINGS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        RATINGS_STATE_FILE.write_text(",".join(sel), encoding="utf-8")
    except Exception:
        pass


def clear_stock_images():
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    try:
        for d in (STOCK_DIR, LANDSCAPE_STOCK_DIR):
            try:
                for p in list(d.iterdir()):
                    try:
                        if p.is_file() and p.suffix.lower() in exts:
                            p.unlink()
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass


def load_rating_selection():
    """Return list of selected rating keys in order [safe,questionable,explicit].

    If config file exists, it overrides defaults. Env var YANDERE_RATINGS may also be
    provided as comma-separated keys (e.g. "questionable,explicit").
    """
    defaults = {"safe": 0, "questionable": 1, "explicit": 1}

    # Start with defaults
    sel = defaults.copy()

    # Read config file if present (search candidates)
    try:
        cfg_path = None
        for candidate in CONFIG_CANDIDATES:
            try:
                if candidate.exists():
                    cfg_path = candidate
                    break
            except Exception:
                continue
        if cfg_path:
            for ln in cfg_path.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                if "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                k = k.strip().lower()
                v = v.strip()
                if k in sel:
                    try:
                        sel[k] = 1 if int(v) else 0
                    except Exception:
                        sel[k] = 1 if v.lower() in {"1", "true", "yes", "on"} else 0
    except Exception:
        pass

    # Environment override: comma-separated keys or key=value pairs
    try:
        env = os.environ.get("YANDERE_RATINGS")
        if env:
            # support both 'k1,k2' and 'k1=1,k2=0' syntaxes
            parts = [p.strip() for p in env.split(",") if p.strip()]
            for p in parts:
                if "=" in p:
                    k, v = p.split("=", 1)
                    k = k.strip().lower()
                    if k in sel:
                        try:
                            sel[k] = 1 if int(v) else 0
                        except Exception:
                            sel[k] = 1 if v.lower() in {"1", "true", "yes", "on"} else 0
                else:
                    k = p.lower()
                    if k in sel:
                        sel[k] = 1
    except Exception:
        pass

    order = ["safe", "questionable", "explicit"]
    return [k for k in order if sel.get(k)]


# Compute selected ratings at import time
SELECTED_RATINGS = load_rating_selection()
# Use COLLAGE_MODE setting regardless of number of selected ratings
# (rating selection just determines what images are downloaded, not collage behavior)
EFFECTIVE_COLLAGE_MODE = COLLAGE_MODE


STATE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
STOCK_DIR.mkdir(parents=True, exist_ok=True)
LANDSCAPE_STOCK_DIR.mkdir(parents=True, exist_ok=True)


def cleanup_partial_files():
    """Remove partial download files left by interrupted runs (.part)."""
    try:
        for p in CACHE_DIR.glob("wallpaper-*.part"):
            try:
                p.unlink()
            except Exception:
                pass
        for p in STOCK_DIR.glob("wallpaper-*.part"):
            try:
                p.unlink()
            except Exception:
                pass
        for p in LANDSCAPE_STOCK_DIR.glob("wallpaper-*.part"):
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        pass


def run_ok(cmd):
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def run_and_log(cmd):
    """Run a command, return True on success."""
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        return proc.returncode == 0
    except Exception:
        return False


def output(cmd):
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""


def set_wallpaper(image: Path) -> bool:
    image_str = str(image.resolve())
    local_for_gsettings = image
    conv_file = None
    try:
        if shutil.which("gsettings") and PIL_AVAILABLE:
            from PIL import Image as _Image
            try:
                with _Image.open(image) as _im:
                    mode = _im.mode
                    needs = False
                    if mode != "RGB":
                        needs = True
                    if mode in ("RGBA", "LA") or _im.info.get("transparency") is not None:
                        needs = True
                    if needs:
                        conv = CACHE_DIR / f"wallpaper-gsettings-{int(time.time())}-{random.randint(1000,9999)}.jpg"
                        # Flatten alpha onto black background
                        try:
                            if _im.mode in ("RGBA", "LA") or _im.info.get("transparency") is not None:
                                bg = _Image.new("RGB", _im.size, (0, 0, 0))
                                bg.paste(_im.convert("RGBA"), mask=_im.convert("RGBA").split()[-1])
                                bg.save(conv, quality=95)
                            else:
                                _im.convert("RGB").save(conv, quality=95)
                            local_for_gsettings = conv
                            conv_file = conv
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass

    uri = f"file://{str(local_for_gsettings)}"

    try:
        if shutil.which("gsettings"):
            schemas = output(["gsettings", "list-schemas"])
            if "org.gnome.desktop.background" in schemas:
                if run_and_log(["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri]):
                    run_and_log(["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri])
                    try:
                        run_and_log(["gsettings", "set", "org.gnome.desktop.background", "picture-options", "zoom"])
                    except Exception:
                        pass
                    return True

        if shutil.which("swaymsg") and os.environ.get("SWAYSOCK"):
            if run_and_log(["swaymsg", "output", "*", "bg", image_str, "fill"]):
                return True

        if shutil.which("feh"):
            if run_and_log(["feh", "--bg-fill", image_str]):
                return True

        if shutil.which("xfconf-query"):
            run_and_log(["xfconf-query", "-c", "xfce4-desktop", "-p", "/backdrop/screen0/monitor0/image-path", "-s", image_str])
            run_and_log(["xfconf-query", "-c", "xfce4-desktop", "-p", "/backdrop/screen0/monitor0/workspace0/last-image", "-s", image_str])
            return True

        if shutil.which("nitrogen"):
            if run_and_log(["nitrogen", "--set-zoom-fill", image_str, "--save"]):
                return True

        log(f"set_wallpaper: no supported method succeeded for {image_str}")
        return False
    finally:
        if conv_file is not None and conv_file.exists():
            try:
                conv_file.unlink()
            except Exception:
                pass



def remaining_time(deadline: float) -> float:
    return deadline - time.monotonic()


def timeout_for(deadline: float, cap: float) -> float:
    left = remaining_time(deadline)
    if left <= 0:
        raise TimeoutError("run timeout exceeded")
    return min(cap, max(1.0, left))


@contextmanager
def single_instance_lock():
    """Cross-platform single instance lock using simple PID file"""
    
    def is_alive(pid: int) -> bool:
        """Check if a process is still alive"""
        try:
            if IS_WINDOWS:
                subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}"], stderr=subprocess.DEVNULL)
                return True
            else:
                os.kill(pid, 0)
                return True
        except (OSError, subprocess.CalledProcessError):
            return False

    def terminate_existing(pid: int) -> None:
        """Kill an existing process"""
        if pid <= 1 or pid == os.getpid():
            return
        
        if not is_alive(pid):
            return
        
        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
                # Give it a moment to die
                for _ in range(20):  # Wait up to 1 second
                    if not is_alive(pid):
                        break
                    time.sleep(0.05)
                # If still alive, force kill
                if is_alive(pid):
                    os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
        
        # After killing, remove any leftover partial files
        try:
            cleanup_partial_files()
        except Exception:
            pass

    # Create lock file directory
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    # Simple PID-based locking - works on all platforms
    max_attempts = 3
    for attempt in range(max_attempts):
        # Read existing lock if it exists
        existing_pid = None
        if LOCK_FILE.exists():
            try:
                existing_pid_raw = LOCK_FILE.read_text(encoding="utf-8").strip()
                existing_pid = int(existing_pid_raw)
            except Exception:
                pass
        
        # If there's an existing PID and it's alive, terminate it
        if existing_pid and existing_pid != os.getpid():
            if is_alive(existing_pid):
                log(f"Terminating existing process (PID {existing_pid})")
                terminate_existing(existing_pid)
                time.sleep(0.2)  # Wait for it to die
            
            # Remove the stale lock file
            try:
                LOCK_FILE.unlink()
            except FileNotFoundError:
                pass
        
        # Try to write our PID as the lock
        try:
            # Use atomic write via temp file if possible
            temp_lock = LOCK_FILE.parent / f"{LOCK_FILE.name}.tmp"
            temp_lock.write_text(str(os.getpid()), encoding="utf-8")
            temp_lock.replace(LOCK_FILE)
            
            # Verify we actually got the lock (our PID is in the file)
            lock_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            if lock_pid == os.getpid():
                # We have the lock
                try:
                    cleanup_partial_files()
                except Exception:
                    pass
                yield
                # Release lock
                try:
                    LOCK_FILE.unlink()
                except FileNotFoundError:
                    pass
                return
        except Exception as e:
            log(f"Lock attempt {attempt + 1} failed: {e}")
            time.sleep(0.1)
    
    # Could not acquire lock after retries
    raise RuntimeError("could not acquire lock after multiple attempts")


def fetch_json(url: str, deadline: float):
    # Use GLOBAL_OPENER to maintain cookies and custom headers
    headers = DEFAULT_HEADERS.copy()
    req = urllib.request.Request(url, headers=headers)
    with GLOBAL_OPENER.open(req, timeout=timeout_for(deadline, 20.0)) as resp:
        raw = resp.read()
        if is_cloudflare_response(raw):
            log(f"Cloudflare/anti-bot detected when fetching JSON from {url}")
            raise RuntimeError("cloudflare or anti-bot page detected")
        return json.loads(raw.decode("utf-8", errors="replace"))


def fetch_moebooru_posts(base_url: str, page: int, rating_tag: str, deadline: float):
    params = urllib.parse.urlencode({
        "limit": 100,
        "page": page,
        "tags": rating_tag,
    })
    url = f"{base_url}/post.json?{params}"
    try:
        return fetch_json(url, deadline)
    except RuntimeError as e:
        # If Konachan triggers Cloudflare, attempt to warm session and retry with header variants
        if "cloudflare" in str(e).lower() and "konachan" in base_url:
            if warm_site(base_url, deadline):
                for hdrs in HEADER_VARIANTS:
                    try:
                        req = urllib.request.Request(url, headers=hdrs)
                        with GLOBAL_OPENER.open(req, timeout=timeout_for(deadline, 20.0)) as resp:
                            raw = resp.read()
                            if is_cloudflare_response(raw):
                                continue
                            return json.loads(raw.decode("utf-8", errors="replace"))
                    except Exception:
                        continue
        raise


def fetch_gelbooru_posts(base_url: str, page: int, rating_tag: str, deadline: float):
    params = urllib.parse.urlencode({
        "page": "dapi",
        "s": "post",
        "q": "index",
        "json": "1",
        "limit": 100,
        "pid": max(0, page - 1),
        "tags": rating_tag,
    })
    data = fetch_json(f"{base_url}/index.php?{params}", deadline)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        posts = data.get("post", [])
        if isinstance(posts, list):
            return posts
        if isinstance(posts, dict):
            return [posts]
    return []


def normalize_ext(file_url: str, fallback: str = "jpg") -> str:
    path = urllib.parse.urlsplit(file_url).path
    ext = Path(path).suffix.lower().lstrip(".")
    if ext in {"jpg", "jpeg", "png", "webp"}:
        return ext
    return fallback


def list_stock_images():
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    images = [p for p in STOCK_DIR.iterdir() if p.is_file() and p.suffix.lower() in exts]
    return images


def delete_half_stock():
    """Delete half of the downloaded stock images."""
    images = list_stock_images()
    if len(images) <= 1:
        return 0
    
    # Shuffle to randomly select which half to delete
    random.shuffle(images)
    delete_count = len(images) // 2
    deleted = 0
    
    for i in range(delete_count):
        try:
            images[i].unlink()
            deleted += 1
        except Exception:
            pass
    
    log(f"Deleted {deleted} half of stock images ({len(images)} -> {len(images) - deleted})")
    return deleted


def list_landscape_stock():
    """Get list of landscape images in Mode 2 stock"""
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    images = [p for p in LANDSCAPE_STOCK_DIR.iterdir() if p.is_file() and p.suffix.lower() in exts]
    return images


def refill_mode_2_stock(deadline: float) -> None:
    """Refill both portrait and landscape stock for Mode 2."""
    if not ALLOW_DOWNLOADS:
        return
    portrait_target, _ = get_mode2_targets()
    portrait_stock = list_stock_images()
    portrait_needed = portrait_target - len(portrait_stock)
    if portrait_needed > 0 and remaining_time(deadline) > 10:
        log(f"Mode 2: Refilling portrait stock ({len(portrait_stock)}/{portrait_target}), need {portrait_needed}")
        portraits = pick_images(portrait_needed, "portrait", deadline)
        if portraits:
            added = 0
            for idx, (ext, url, _source_name, _rating) in enumerate(portraits, start=1):
                if remaining_time(deadline) <= 0:
                    break
                try:
                    download_file(
                        url,
                        ext,
                        deadline,
                        STOCK_DIR,
                        progress_label=progress_label_for(f"portrait {idx}"),
                    )
                    added += 1
                except Exception:
                    pass
            if added > 0:
                log(f"Mode 2: Added {added} portraits to stock")

    refill_landscape_stock(deadline)


def refill_landscape_stock(deadline: float) -> int:
    """Ensure Mode 2 landscape cache stays near its target size."""
    if not ALLOW_DOWNLOADS:
        return 0
    _, target_count = get_mode2_targets()
    current = len(list_landscape_stock())
    needed = target_count - current
    
    if needed <= 0:
        return 0
    
    log(f"Refilling landscape stock: have {current}, need {needed} more (target: {target_count})")
    
    landscapes = pick_images(needed, "landscape", deadline)
    if not landscapes:
        return 0

    added = 0
    for idx, (ext, url, _source_name, _rating) in enumerate(landscapes, start=1):
        if remaining_time(deadline) <= 0:
            break
        try:
            file = download_file(
                url,
                ext,
                deadline,
                LANDSCAPE_STOCK_DIR,
                progress_label=progress_label_for(f"landscape {idx}"),
            )
            added += 1
        except Exception as e:
            log(f"Failed to download landscape for stock: {e}")
    
    log(f"Added {added} landscape images to stock")
    return added


def fetch_random_posts(deadline: float, orientation: str = None):
    for _ in range(4):
        if remaining_time(deadline) <= 0:
            break
        # Restrict booru sources by orientation: use konachan only for landscapes,
        # use yande.re only for portraits. Fall back to all sources if filtering fails.
        try:
            if orientation == "landscape":
                candidates = [s for s in BOORU_SOURCES if "konachan" in s["name"]]
            elif orientation == "portrait":
                candidates = [s for s in BOORU_SOURCES if "yande" in s["name"]]
            else:
                candidates = BOORU_SOURCES
            if not candidates:
                candidates = BOORU_SOURCES
        except Exception:
            candidates = BOORU_SOURCES
        source = random.choice(candidates)
        # Determine rating tags to use for this source based on configuration
        if SELECTED_RATINGS:
            mapping = RATING_TAGS_MAP.get(source["kind"], {})
            rating_tags = [mapping.get(r) for r in SELECTED_RATINGS if mapping.get(r)]
            if not rating_tags:
                rating_tags = list(RATING_TAGS.get(source["kind"], []))
        else:
            rating_tags = list(RATING_TAGS[source["kind"]])
        random.shuffle(rating_tags)
        page = random.randint(1, max(1, MAX_API_PAGE))
        merged_posts = []
        used_ratings = []
        for rating_tag in rating_tags:
            try:
                if source["kind"] == "moebooru":
                    posts = fetch_moebooru_posts(source["base"], page, rating_tag, deadline)
                else:
                    posts = fetch_gelbooru_posts(source["base"], page, rating_tag, deadline)
                if posts:
                    merged_posts.extend(posts)
                    used_ratings.append(rating_tag)
            except Exception:
                time.sleep(min(0.3, max(0.0, remaining_time(deadline))))
                continue
        if merged_posts:
            return source["name"], "+".join(used_ratings), merged_posts
    # If nothing found, and this was a landscape request, try yande.re explicitly
    if orientation == "landscape":
        try:
            source = next((s for s in BOORU_SOURCES if "yande.re" in s["name"]), None)
            if source:
                page = random.randint(1, max(1, MAX_API_PAGE))
                mapping = RATING_TAGS_MAP.get(source["kind"], {})
                rating_tags = [mapping.get(r) for r in SELECTED_RATINGS if mapping.get(r)] if SELECTED_RATINGS else list(RATING_TAGS.get(source["kind"], []))
                if not rating_tags:
                    rating_tags = list(RATING_TAGS.get(source["kind"], []))
                random.shuffle(rating_tags)
                merged_posts = []
                used_ratings = []
                for rating_tag in rating_tags:
                    try:
                        if source["kind"] == "moebooru":
                            posts = fetch_moebooru_posts(source["base"], page, rating_tag, deadline)
                        else:
                            posts = fetch_gelbooru_posts(source["base"], page, rating_tag, deadline)
                        if posts:
                            merged_posts.extend(posts)
                            used_ratings.append(rating_tag)
                    except Exception:
                        time.sleep(min(0.3, max(0.0, remaining_time(deadline))))
                        continue
                if merged_posts:
                    return source["name"], "+".join(used_ratings), merged_posts
        except Exception:
            pass

    return None, None, []


def pick_images(count: int, orientation: str, deadline: float):
    collected = []
    seen_urls = set()

    for _ in range(40):
        if remaining_time(deadline) <= 0:
            break
        source_name, rating_tag, posts = fetch_random_posts(deadline, orientation)
        if not posts:
            continue
        candidates = []
        for p in posts:
            try:
                w = int(p.get("width", 0))
                h = int(p.get("height", 0))
            except Exception:
                continue
            if orientation == "landscape":
                if w <= h or w < MIN_WIDTH or h < MIN_HEIGHT:
                    continue
            elif orientation == "portrait":
                # For portrait: use any image where width is not greater than height (portrait or square)
                if w > h:
                    continue
            else:
                continue
            file_url = p.get("file_url")
            if not file_url:
                continue
            if file_url.startswith("//"):
                file_url = f"https:{file_url}"
            if file_url in seen_urls:
                continue
            ext = normalize_ext(file_url, (p.get("file_ext") or "jpg").lower())
            candidates.append((w * h, ext, file_url, source_name, rating_tag))

        if not candidates:
            continue

        candidates.sort(key=lambda x: x[0], reverse=True)
        pool = candidates[:30]
        while pool and len(collected) < count:
            _, ext, url, s_name, rating = random.choice(pool)
            pool = [c for c in pool if c[2] != url]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            collected.append((ext, url, s_name, rating))

        if len(collected) >= count:
            return collected[:count]

    return collected


def compose_collage(image_paths, target_path: Path) -> Path:
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow is not installed (required for collage mode).")
    if len(image_paths) < 2:
        raise RuntimeError("Need at least 2 images to make a collage.")

    # Enable loading of truncated images and disable decompression bomb check
    from PIL import ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    Image.MAX_IMAGE_PIXELS = None

    count = len(image_paths)
    cols = count
    tile_h = TARGET_HEIGHT
    base_w = TARGET_WIDTH // cols
    remainder = TARGET_WIDTH % cols
    widths = [base_w + (1 if i < remainder else 0) for i in range(cols)]

    canvas = Image.new("RGB", (TARGET_WIDTH, tile_h))
    resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS

    for idx, path in enumerate(image_paths):
        tile_w = widths[idx]
        with Image.open(path) as im:
            tile = ImageOps.fit(im.convert("RGB"), (tile_w, tile_h), method=resample)
        x = sum(widths[:idx])
        y = 0
        canvas.paste(tile, (x, y))

    canvas.save(target_path, quality=95)
    return target_path


def choose_collage_count(stock_count: int) -> int:
    if stock_count < 2:
        return 0
    max_columns = max(2, TARGET_WIDTH // MIN_TILE_WIDTH)
    return min(stock_count, max_columns)


def handle_mode_2_random(deadline: float) -> int:
    """
    Mode 2: Alternating between collage (from portrait stock) and single landscapes.
    Retains both portrait and landscape caches (portrait target = `STOCK_TARGET`,
    landscapes ≈ portrait target / 3) and strictly alternates the output type.
    """
    log_state = read_mode2_log()
    last = log_state["action"]
    want_collage = last == "landscape"

    portrait_stock = list_stock_images()
    landscape_stock = list_landscape_stock()
    log(
        f"Mode 2: portraits={len(portrait_stock)}, landscapes={len(landscape_stock)}, "
        f"want_collage={want_collage}, last={repr(last)}, "
        f"total_landscapes={log_state['total_landscapes']}, total_collages={log_state['total_collages']}"
    )
    if want_collage:
        if len(portrait_stock) < 3 and remaining_time(deadline) > 10:
            log("Mode 2: Portrait stock low; refilling portraits...")
            refill_mode_2_stock(deadline)
            portrait_stock = list_stock_images()
            landscape_stock = list_landscape_stock()
    else:
        if len(landscape_stock) < 1 and remaining_time(deadline) > 10:
            log("Mode 2: Landscape stock low; refilling landscapes...")
            refill_mode_2_stock(deadline)
            portrait_stock = list_stock_images()
            landscape_stock = list_landscape_stock()

    if want_collage:
        if len(portrait_stock) >= 3:
            log("Mode 2: Creating collage from stock (3 portraits)...")
            try:
                used_portraits = random.sample(portrait_stock, 3)
                collage_target = USED_WALLPAPER_DIR / f"wallpaper-mode2-{int(time.time())}-{random.randint(1000,9999)}.jpg"
                collage_file = compose_collage(used_portraits, collage_target)
                collage_file = number_wallpaper(collage_file)
                if set_wallpaper(collage_file):
                    STATE_FILE.write_text(str(collage_file), encoding="utf-8")
                    log("Mode 2: Collage wallpaper set from stock")
                    for p in used_portraits:
                        try:
                            if p.exists():
                                p.unlink()
                        except Exception:
                            pass
                    refill_mode_2_stock(deadline)
                    write_mode2_log("collage")
                    return 0
                else:
                    try:
                        if collage_file.exists():
                            collage_file.unlink()
                    except Exception:
                        pass
            except Exception as e:
                log(f"Failed to create collage from stock: {e}")
        log("Mode 2: Collage not possible, falling back to landscape...")

    if len(landscape_stock) >= 1:
        log("Mode 2: Using single landscape from stock...")
        selected_landscape = random.choice(landscape_stock)
        numbered = number_wallpaper(selected_landscape)
        if set_wallpaper(numbered):
            STATE_FILE.write_text(str(numbered), encoding="utf-8")
            log("Mode 2: Landscape wallpaper set from stock")
            try:
                if selected_landscape.exists():
                    selected_landscape.unlink()
            except Exception:
                pass
            refill_mode_2_stock(deadline)
            write_mode2_log("landscape")
            return 0
        log("Failed to set landscape wallpaper.")
        return 1

    log("No landscape stock available, downloading one...")
    if not ALLOW_DOWNLOADS:
        log("Downloads disabled; helper should refill stock.")
        return 1
    landscapes = pick_images(1, "landscape", deadline)
    if not landscapes:
        log("Could not find a suitable landscape image.")
        return 1
    ext, url, source_name, rating = landscapes[0]
    try:
        landscape_file = download_file(
            url,
            ext,
            deadline,
            LANDSCAPE_STOCK_DIR,
            progress_label=progress_label_for("landscape"),
        )
        numbered_landscape = number_wallpaper(landscape_file)
        if set_wallpaper(numbered_landscape):
            STATE_FILE.write_text(str(numbered_landscape), encoding="utf-8")
            log("Mode 2: Landscape wallpaper set")
            refill_mode_2_stock(deadline)
            write_mode2_log("landscape")
            return 0
        log("Could not set wallpaper.")
        return 1
    except Exception as e:
        log(str(e))
        return 1


def download_file(
    url: str,
    ext: str,
    deadline: float,
    out_dir: Path = CACHE_DIR,
    retries: int = 3,
    request_timeout_cap: float = 30.0,
    progress_label: str | None = None,
) -> Path:
    filename = f"wallpaper-{int(time.time())}-{random.randint(1000,9999)}.{ext}"
    target = out_dir / filename
    target_tmp = out_dir / (filename + ".part")
    # Use GLOBAL_OPENER to maintain cookies and custom headers
    headers = DEFAULT_HEADERS.copy()
    # Provide referer to appear like a browser
    try:
        headers["Referer"] = urllib.parse.urlsplit(url)._replace(query="").geturl()
    except Exception:
        pass
    req = urllib.request.Request(url, headers=headers)
    last_err = None
    for _ in range(max(1, retries)):
        reporter = DownloadProgress(progress_label) if progress_label else None
        try:
            # Ensure directory exists
            out_dir.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(req, timeout=timeout_for(deadline, request_timeout_cap)) as resp, open(target_tmp, "wb") as f:
                total_size = None
                if reporter:
                    try:
                        header_value = resp.getheader("Content-Length")
                        total_size = int(header_value) if header_value else None
                    except Exception:
                        total_size = None
                    reporter.start(total_size)
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    if reporter:
                        reporter.update(len(chunk))
            try:
                target_tmp.replace(target)
            except Exception:
                # Fallback to rename
                try:
                    os.replace(str(target_tmp), str(target))
                except Exception:
                    pass
            return target
        except Exception as e:
            last_err = e
            try:
                if target_tmp.exists():
                    target_tmp.unlink()
            except Exception:
                pass
            time.sleep(min(0.5, max(0.0, remaining_time(deadline))))
        finally:
            if reporter:
                reporter.finish()

    raise RuntimeError(f"download failed: {last_err}")


def download_many(
    items,
    deadline: float,
    out_dir: Path = CACHE_DIR,
    retries: int = 3,
    request_timeout_cap: float = 30.0,
):
    if not items:
        return []

    if SHOW_DOWNLOAD_PROGRESS:
        results = []
        for idx, (ext, url, _source_name, _rating) in enumerate(items):
            label = f"image {idx + 1}"
            try:
                results.append(download_file(url, ext, deadline, out_dir, retries, request_timeout_cap, progress_label=label))
            except Exception as e:
                log(f"Download failed: {e}")
        return results

    results = []
    workers = min(DOWNLOAD_THREADS, len(items))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(download_file, url, ext, deadline, out_dir, retries, request_timeout_cap): idx
            for idx, (ext, url, _source_name, _rating) in enumerate(items)
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                log(f"Download failed: {e}")

    return results


def refill_stock(deadline: float, quick: bool = False):
    current = list_stock_images()
    if not ALLOW_DOWNLOADS:
        return
    need = max(0, STOCK_TARGET - len(current))
    if need <= 0:
        return
    log(f"Stock: {len(current)}/{STOCK_TARGET}, downloading {need} more.")
    picked = pick_images(need, "portrait", deadline)
    if not picked:
        return
    if quick:
        download_many(picked, deadline, STOCK_DIR, retries=1, request_timeout_cap=8.0)
    else:
        download_many(picked, deadline, STOCK_DIR)


def run_wallpaper() -> int:
    ensure_helper_process()
    start = time.monotonic()
    deadline = start + RUN_TIMEOUT
    stop_event = Event()
    timer_thread = None
    if SHOW_COUNTDOWN:
        timer_thread = Thread(target=countdown_worker, args=(deadline, stop_event), daemon=True)
        timer_thread.start()

    lock_cm = None
    try:
        lock_cm = single_instance_lock()
        lock_cm.__enter__()
    except Exception as e:
        log(str(e))
        code = 0
    else:
        try:
            code = _main_with_lock(deadline)
        finally:
            try:
                lock_cm.__exit__(None, None, None)
            except Exception:
                pass

    stop_event.set()
    if timer_thread is not None:
        timer_thread.join(timeout=1.0)
    elapsed = time.monotonic() - start
    log(f"Total time: {elapsed:.2f}s")
    return code


def _main_with_lock(deadline: float) -> int:
    old_wallpaper = None
    if STATE_FILE.exists():
        try:
            old_wallpaper = STATE_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            old_wallpaper = None

    # Log active ratings and mode for debugging
    try:
        mode_desc = {0: "landscape-only stock", 1: "stock-based collage", 2: "alternating collage/landscape"}.get(COLLAGE_MODE, f"unknown({COLLAGE_MODE})")
        log(f"Active ratings: {SELECTED_RATINGS} | Collage mode: {mode_desc}")
    except Exception:
        pass

    # If selected ratings have changed since last run, clear stock and download fresh ones
    try:
        prev_ratings = load_saved_ratings()
        if prev_ratings != SELECTED_RATINGS:
            log(f"Rating selection changed: {prev_ratings} -> {SELECTED_RATINGS}; clearing stock and refilling.")
            clear_stock_images()
            # Refill appropriate stock depending on collage mode
            try:
                if COLLAGE_MODE == 2:
                    refill_mode_2_stock(deadline)
                else:
                    refill_stock(deadline)
            except Exception:
                pass
            save_selected_ratings(SELECTED_RATINGS)
    except Exception:
        pass

    new_file = None

    # Mode 0: Landscape-only stock mode
    if COLLAGE_MODE == 0:
        landscape_stock = list_landscape_stock()
        
        # Refill landscape stock if low
        if len(landscape_stock) < 1 and remaining_time(deadline) > 10:
            log("Mode 0: Refilling landscape stock...")
            landscapes = pick_images(max(3, STOCK_TARGET // 4), "landscape", deadline)
            if landscapes:
                added = 0
                for idx, (ext, url, _source_name, _rating) in enumerate(landscapes, start=1):
                    if remaining_time(deadline) <= 0:
                        break
                    try:
                        download_file(
                            url,
                            ext,
                            deadline,
                            LANDSCAPE_STOCK_DIR,
                            progress_label=progress_label_for(f"landscape {idx}"),
                        )
                        added += 1
                    except Exception:
                        pass
                if added > 0:
                    log(f"Mode 0: Added {added} landscapes to stock")
            landscape_stock = list_landscape_stock()
        
        # Use a landscape from stock
        if len(landscape_stock) >= 1:
            selected = random.choice(landscape_stock)
            numbered = number_wallpaper(selected)
            if set_wallpaper(numbered):
                STATE_FILE.write_text(str(numbered), encoding="utf-8")
                log("Mode 0: Landscape wallpaper set")
                
                
                # Refill stock for next run
                try:
                    landscapes = pick_images(max(1, STOCK_TARGET // 4), "landscape", deadline)
                    if landscapes:
                        for idx, (ext, url, _source_name, _rating) in enumerate(landscapes, start=1):
                            if remaining_time(deadline) <= 0:
                                break
                            try:
                                download_file(
                                    url,
                                    ext,
                                    deadline,
                                    LANDSCAPE_STOCK_DIR,
                                    progress_label=progress_label_for(f"landscape {idx}"),
                                )
                            except Exception:
                                pass
                except Exception:
                    pass
                return 0
            else:
                log("Failed to set landscape wallpaper.")
                return 1
        else:
            log("No landscape stock available and could not download.")
            return 1
    
    # Mode 2: Alternating collage/landscape selection with maintained stock
    if COLLAGE_MODE == 2:
        return handle_mode_2_random(deadline)
    
    # Mode 1: Stock-based collage mode
    if COLLAGE_MODE == 1:
        max_collage_attempts = 10
        for attempt in range(max_collage_attempts):
            stock_images = list_stock_images()
            desired_count = choose_collage_count(len(stock_images))
            
            # If not enough stock, download more portrait images first
            if desired_count < 3:
                log(f"Not enough stock images ({len(stock_images)}), downloading more...")
                refill_stock(deadline)
                stock_images = list_stock_images()
                desired_count = choose_collage_count(len(stock_images))
                if desired_count < 3:
                    log(f"Still not enough stock ({len(stock_images)}), will use single image instead.")
                    break
            
            if desired_count >= 3:
                used_stock_parts = random.sample(stock_images, desired_count)
                log(f"Building collage from stock: {desired_count} images (available: {len(stock_images)}), attempt {attempt + 1}/{max_collage_attempts}.")
                try:
                    collage_target = USED_WALLPAPER_DIR / f"wallpaper-collage-{int(time.monotonic()) }-{random.randint(1000,9999)}.jpg"
                    new_file = compose_collage(used_stock_parts, collage_target)
                    new_file = number_wallpaper(new_file)
                    
                    # Try to set the wallpaper
                    if set_wallpaper(new_file):
                        # Success! Save state
                        STATE_FILE.write_text(str(new_file), encoding="utf-8")
                        log(f"Wallpaper set successfully")

                        # Delete the stock images used to build the collage (but keep the collage file in cache)
                        for p in used_stock_parts:
                            try:
                                # Ensure we only delete files under STOCK_DIR
                                p.resolve().relative_to(STOCK_DIR.resolve())
                                if p.exists():
                                    p.unlink()
                            except Exception:
                                pass

                        # Refill stock after successful wallpaper set
                        refill_stock(deadline)
                        return 0
                    else:
                        # Wallpaper set failed, delete collage and try again
                        log(f"Failed to set collage wallpaper, trying different images.")
                        try:
                            new_file.unlink()
                        except Exception:
                            pass
                        new_file = None
                except Exception as e:
                    log(f"Collage mode failed, trying different images: {e}")
                    # Don't delete stock files - just skip them for next attempt
                    new_file = None
                    continue

    # Fallback to single image (only if collage completely fails)
    if new_file is None:
        if not ALLOW_DOWNLOADS:
            log("Downloads disabled; helper should refill stock before running.")
            return 1
        picked_single = pick_images(1, "landscape", deadline)
        if not picked_single:
            log("Could not find a suitable image from configured sources.")
            return 1
        ext, url, source_name, rating = picked_single[0]
        log(f"Source: {source_name} ({rating})")
        log(f"Downloading: {url}")
        try:
            new_file = download_file(
                url,
                ext,
                deadline,
                USED_WALLPAPER_DIR,
                progress_label=progress_label_for("image 1"),
            )
            new_file = number_wallpaper(new_file)
        except Exception as e:
            log(str(e))
            return 1

    if not set_wallpaper(new_file):
        log("Could not set wallpaper automatically (unsupported desktop environment).")
        log(f"Image downloaded to: {new_file}")
        return 1

    STATE_FILE.write_text(str(new_file), encoding="utf-8")

    # Do not delete previously set wallpapers; keep history in cache

    log(f"Wallpaper set successfully")
    
    # Refill stock after successful wallpaper set
    refill_stock(deadline)
    return 0
