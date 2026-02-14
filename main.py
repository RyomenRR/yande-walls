#!/usr/bin/env python3
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
from threading import Event, Thread
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Platform detection
IS_WINDOWS = sys.platform == "win32"

try:
    import fcntl
except Exception:
    fcntl = None

try:
    import ctypes
except Exception:
    ctypes = None

try:
    import winreg
except Exception:
    winreg = None

try:
    from PIL import Image, ImageOps
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False


def log(msg: str) -> None:
    print(f"[yandere-wallpaper] {msg}")


def countdown_worker(deadline: float, stop_event: Event) -> None:
    if not sys.stderr.isatty():
        return
    while not stop_event.wait(1):
        left = max(0, int(deadline - time.monotonic() + 0.999))
        sys.stderr.write(f"\r[yandere-wallpaper] Time left: {left:02d}s ")
        sys.stderr.flush()
        if left <= 0:
            break
    sys.stderr.write("\n")
    sys.stderr.flush()


STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "yandere-wallpaper"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "yandere-wallpaper"
STOCK_DIR = CACHE_DIR / "stock"
STATE_FILE = STATE_DIR / "current_wallpaper"
MIN_WIDTH = int(os.environ.get("MIN_WIDTH", "1600"))
MIN_HEIGHT = int(os.environ.get("MIN_HEIGHT", "900"))
MAX_API_PAGE = int(os.environ.get("MAX_API_PAGE", "300"))
TARGET_WIDTH = int(os.environ.get("TARGET_WIDTH", "1920"))
TARGET_HEIGHT = int(os.environ.get("TARGET_HEIGHT", "1080"))
COLLAGE_MODE = os.environ.get("COLLAGE_MODE", "1") not in {"0", "false", "False"}
COLLAGE_MIN_IMAGES = max(2, int(os.environ.get("COLLAGE_MIN_IMAGES", "3")))
COLLAGE_MAX_IMAGES = max(COLLAGE_MIN_IMAGES, int(os.environ.get("COLLAGE_MAX_IMAGES", "4")))
STOCK_TARGET = max(COLLAGE_MAX_IMAGES, int(os.environ.get("STOCK_TARGET", "30")))
PORTRAIT_MIN_WIDTH = int(os.environ.get("PORTRAIT_MIN_WIDTH", "700"))
PORTRAIT_MIN_HEIGHT = int(os.environ.get("PORTRAIT_MIN_HEIGHT", "1000"))
DOWNLOAD_THREADS = max(2, int(os.environ.get("DOWNLOAD_THREADS", "8")))
RUN_TIMEOUT = max(10, int(os.environ.get("RUN_TIMEOUT", "300")))
LOCK_FILE = STATE_DIR / "run.lock"
SHOW_COUNTDOWN = os.environ.get("SHOW_COUNTDOWN", "0") not in {"1", "true", "True"}
MIN_TILE_WIDTH = int(os.environ.get("MIN_TILE_WIDTH", "500"))
BOORU_SOURCES = [
    {"name": "yande.re", "kind": "moebooru", "base": "https://yande.re"},
    {"name": "konachan.com", "kind": "moebooru", "base": "https://konachan.com"},
    {"name": "gelbooru.com", "kind": "gelbooru", "base": "https://gelbooru.com"},
]
RATING_TAGS = {
    "moebooru": ["rating:q", "rating:e"],  # ecchi/questionable + explicit
    "gelbooru": ["rating:questionable", "rating:explicit"],
}

# Mapping of logical rating names to site-specific tag strings
RATING_TAGS_MAP = {
    "moebooru": {
        "safe": "rating:s",
        "questionable": "rating:q",
        "explicit": "rating:e",
    },
    "gelbooru": {
        "safe": "rating:safe",
        "questionable": "rating:questionable",
        "explicit": "rating:explicit",
    },
}

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
        for p in list(STOCK_DIR.iterdir()):
            try:
                if p.is_file() and p.suffix.lower() in exts:
                    p.unlink()
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
# Effective collage mode: if config provided, use it to decide (single if 1 rating, collage if >=2)
# If no selection provided (empty), fall back to COLLAGE_MODE env/default.
if SELECTED_RATINGS:
    EFFECTIVE_COLLAGE_MODE = len(SELECTED_RATINGS) >= 2
else:
    EFFECTIVE_COLLAGE_MODE = COLLAGE_MODE


STATE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
STOCK_DIR.mkdir(parents=True, exist_ok=True)


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
    except Exception:
        pass


def run_ok(cmd):
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def output(cmd):
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""


def set_wallpaper(image: Path) -> bool:
    image_str = str(image)
    
    # Windows implementation
    if IS_WINDOWS:
        return set_wallpaper_windows(image)
    
    # Unix/Linux implementations
    uri = f"file://{image_str}"

    if shutil.which("gsettings"):
        schemas = output(["gsettings", "list-schemas"])
        if "org.gnome.desktop.background" in schemas:
            if run_ok(["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri]):
                run_ok(["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri])
                return True

    if shutil.which("swaymsg") and os.environ.get("SWAYSOCK"):
        if run_ok(["swaymsg", "output", "*", "bg", image_str, "fill"]):
            return True

    if shutil.which("feh"):
        if run_ok(["feh", "--bg-fill", image_str]):
            return True

    if shutil.which("xfconf-query"):
        run_ok(["xfconf-query", "-c", "xfce4-desktop", "-p", "/backdrop/screen0/monitor0/image-path", "-s", image_str])
        run_ok(["xfconf-query", "-c", "xfce4-desktop", "-p", "/backdrop/screen0/monitor0/workspace0/last-image", "-s", image_str])
        return True

    if shutil.which("nitrogen"):
        if run_ok(["nitrogen", "--set-zoom-fill", image_str, "--save"]):
            return True

    return False


def set_wallpaper_windows(image: Path) -> bool:
    """Set wallpaper on Windows using ctypes"""
    if ctypes is None:
        log("ctypes not available, cannot set wallpaper on Windows")
        return False
    
    try:
        image_path = str(image.resolve())
        
        # Try method 1: Using Windows Registry (more reliable, requires admin on some systems)
        if winreg is not None:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
                    winreg.SetValueEx(key, "Wallpaper", 0, winreg.REG_SZ, image_path)
                
                # Notify system of change
                ctypes.windll.user32.SystemParametersInfoW(20, 0, image_path, 3)
                return True
            except Exception as e:
                log(f"Registry method failed: {e}")
        
        # Try method 2: Direct API call
        try:
            result = ctypes.windll.user32.SystemParametersInfoW(20, 0, image_path, 3)
            if result:
                return True
        except Exception as e:
            log(f"Direct API method failed: {e}")
        
        # Try method 3: PowerShell command
        try:
            ps_cmd = (
                f'Add-Type @" '
                f'using System; '
                f'using System.Runtime.InteropServices; '
                f'public class Wallpaper {{ '
                f'[DllImport(\\"user32.dll\\")] public static extern bool SystemParametersInfo(uint uAction, uint uParam, string lpvParam, uint fuWinIni); '
                f'}} '
                f'"@; '
                f'[Wallpaper]::SystemParametersInfo(20, 0, \\"{image_path}\\", 3)'
            )
            subprocess.run(["powershell", "-Command", ps_cmd], check=False, capture_output=True)
            return True
        except Exception as e:
            log(f"PowerShell method failed: {e}")
        
        return False
    except Exception as e:
        log(f"Failed to set wallpaper on Windows: {e}")
        return False


def remaining_time(deadline: float) -> float:
    return deadline - time.monotonic()


def timeout_for(deadline: float, cap: float) -> float:
    left = remaining_time(deadline)
    if left <= 0:
        raise TimeoutError("run timeout exceeded")
    return min(cap, max(1.0, left))


@contextmanager
def single_instance_lock():
    """Cross-platform single instance lock"""
    
    def is_alive(pid: int) -> bool:
        try:
            if IS_WINDOWS:
                import subprocess
                subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}"], stderr=subprocess.DEVNULL)
                return True
            else:
                os.kill(pid, 0)
                return True
        except (OSError, subprocess.CalledProcessError):
            return False

    def looks_like_our_process(pid: int) -> bool:
        """Check if a process is one of our wallpaper scripts"""
        if IS_WINDOWS:
            try:
                output_str = subprocess.check_output(
                    ["tasklist", "/FI", f"PID eq {pid}", "/V"],
                    stderr=subprocess.DEVNULL,
                    text=True
                )
                return "python" in output_str.lower() or "main.py" in output_str
            except Exception:
                return False
        else:
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", errors="ignore")
            except Exception:
                return False
            return ("main.py" in cmdline) or ("yandere.sh" in cmdline)

    def terminate_existing(pid: int) -> None:
        if pid <= 1 or pid == os.getpid():
            return
        if not looks_like_our_process(pid):
            return

        try:
            if IS_WINDOWS:
                import subprocess
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            return

        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            if not is_alive(pid):
                return
            time.sleep(0.05)

        # After killing an existing run, remove any leftover partial files
        try:
            cleanup_partial_files()
        except Exception:
            pass

    # Platform-specific locking implementation
    if IS_WINDOWS:
        # Windows: use simple file-based locking
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        lock_fp = open(LOCK_FILE, "a+", encoding="utf-8")
        
        try:
            # Read existing PID
            lock_fp.seek(0)
            existing_pid_raw = lock_fp.read().strip()
            try:
                existing_pid = int(existing_pid_raw)
            except Exception:
                existing_pid = -1
            
            # Try to terminate existing process
            if existing_pid > 0:
                terminate_existing(existing_pid)
            
            # Write our PID
            lock_fp.seek(0)
            lock_fp.truncate()
            lock_fp.write(str(os.getpid()))
            lock_fp.flush()
            
            # Clean up any lingering partial downloads
            try:
                cleanup_partial_files()
            except Exception:
                pass
            
            yield
        finally:
            try:
                lock_fp.close()
            except Exception:
                pass
    
    elif fcntl is not None:
        # Unix: use fcntl file locking
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        lock_fp = open(LOCK_FILE, "a+", encoding="utf-8")
        
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_fp.seek(0)
            existing_pid_raw = lock_fp.read().strip()
            try:
                existing_pid = int(existing_pid_raw)
            except Exception:
                existing_pid = -1

            terminate_existing(existing_pid)

            acquired = False
            end = time.monotonic() + 2.0
            while time.monotonic() < end:
                try:
                    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    time.sleep(0.05)
            if not acquired:
                lock_fp.close()
                raise RuntimeError("could not preempt existing run")
        
        try:
            lock_fp.seek(0)
            lock_fp.truncate()
            lock_fp.write(str(os.getpid()))
            lock_fp.flush()
            # Clean up any lingering partial downloads from previous runs
            try:
                cleanup_partial_files()
            except Exception:
                pass
            yield
        finally:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            lock_fp.close()
    else:
        # No locking available, just yield
        yield


def fetch_json(url: str, deadline: float):
    req = urllib.request.Request(url, headers={"User-Agent": "yandere-wallpaper-script/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_for(deadline, 20.0)) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_moebooru_posts(base_url: str, page: int, rating_tag: str, deadline: float):
    params = urllib.parse.urlencode({
        "limit": 100,
        "page": page,
        "tags": rating_tag,
    })
    return fetch_json(f"{base_url}/post.json?{params}", deadline)


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


def fetch_random_posts(deadline: float):
    for _ in range(4):
        if remaining_time(deadline) <= 0:
            break
        source = random.choice(BOORU_SOURCES)
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
    return None, None, []


def pick_images(count: int, orientation: str, deadline: float):
    collected = []
    seen_urls = set()

    for _ in range(40):
        if remaining_time(deadline) <= 0:
            break
        source_name, rating_tag, posts = fetch_random_posts(deadline)
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
    # Always use exactly 3 images for collage
    if stock_count >= 3:
        return 3
    return 0


def download_file(
    url: str,
    ext: str,
    deadline: float,
    out_dir: Path = CACHE_DIR,
    retries: int = 3,
    request_timeout_cap: float = 30.0,
) -> Path:
    filename = f"wallpaper-{int(time.time())}-{random.randint(1000,9999)}.{ext}"
    target = out_dir / filename
    target_tmp = out_dir / (filename + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "yandere-wallpaper-script/1.0"})

    last_err = None
    for _ in range(max(1, retries)):
        try:
            # Ensure directory exists
            out_dir.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(req, timeout=timeout_for(deadline, request_timeout_cap)) as resp, open(target_tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
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


def main() -> int:
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
        log(f"Active ratings: {SELECTED_RATINGS} | Collage mode: {EFFECTIVE_COLLAGE_MODE}")
    except Exception:
        pass

    # If selected ratings have changed since last run, clear stock and download fresh ones
    try:
        prev_ratings = load_saved_ratings()
        if prev_ratings != SELECTED_RATINGS:
            log(f"Rating selection changed: {prev_ratings} -> {SELECTED_RATINGS}; clearing stock and refilling.")
            clear_stock_images()
            # refill immediately using current deadline
            try:
                refill_stock(deadline)
            except Exception:
                pass
            save_selected_ratings(SELECTED_RATINGS)
    except Exception:
        pass

    new_file = None

    # Try collage mode with retry on failure
    if EFFECTIVE_COLLAGE_MODE:
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
                    collage_target = CACHE_DIR / f"wallpaper-collage-{int(time.time())}-{random.randint(1000,9999)}.jpg"
                    new_file = compose_collage(used_stock_parts, collage_target)
                    
                    # Try to set the wallpaper
                    if set_wallpaper(new_file):
                        # Success! Save state
                        STATE_FILE.write_text(str(new_file), encoding="utf-8")
                        log(f"Wallpaper set successfully: {new_file}")

                        # Delete the stock images used to build the collage (but keep the collage file)
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
        picked_single = pick_images(1, "landscape", deadline)
        if not picked_single:
            log("Could not find a suitable image from configured sources.")
            return 1
        ext, url, source_name, rating = picked_single[0]
        log(f"Source: {source_name} ({rating})")
        log(f"Downloading: {url}")
        try:
            new_file = download_file(url, ext, deadline)
        except Exception as e:
            log(str(e))
            return 1

    if not set_wallpaper(new_file):
        log("Could not set wallpaper automatically (unsupported desktop environment).")
        log(f"Image downloaded to: {new_file}")
        return 1

    STATE_FILE.write_text(str(new_file), encoding="utf-8")

    # Do not delete previously set wallpapers; keep history in cache

    log(f"Wallpaper set successfully: {new_file}")
    
    # Refill stock after successful wallpaper set
    refill_stock(deadline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
