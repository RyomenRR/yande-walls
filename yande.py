#!/usr/bin/env python3
import os
import sys
import time
from main import (
    ensure_helper_process,
    log,
    reset_next_wallpaper_timer,
    run_wallpaper,
    set_allow_downloads,
    single_instance_lock,
    SLIDESHOW_MINUTES,
    start_slideshow_timer,
    stop_slideshow_timer,
)


FORCE_DOWNLOADS = os.environ.get("FORCE_DOWNLOADS", "").lower() in {"1", "true", "yes"}


def main_entry() -> int:
    set_allow_downloads(FORCE_DOWNLOADS)
    reset_next_wallpaper_timer()
    if SLIDESHOW_MINUTES > 0:
        interval = max(1, SLIDESHOW_MINUTES) * 60
        lock_cm = single_instance_lock()
        try:
            lock_cm.__enter__()
        except Exception as exc:
            log(str(exc))
            return 0
        try:
            ensure_helper_process()
            while True:
                code = run_wallpaper(acquire_lock=False)
                stop_event, timer_thread = start_slideshow_timer(interval)
                try:
                    time.sleep(interval)
                finally:
                    stop_slideshow_timer(stop_event, timer_thread)
        except KeyboardInterrupt:
            log("Slideshow interrupted by user")
            return 0
        finally:
            try:
                lock_cm.__exit__(None, None, None)
            except Exception:
                pass
        return 0
    lock_cm = single_instance_lock()
    try:
        lock_cm.__enter__()
    except Exception as exc:
        log(str(exc))
        return 0
    try:
        ensure_helper_process()
        return run_wallpaper(acquire_lock=False)
    finally:
        try:
            lock_cm.__exit__(None, None, None)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main_entry())
