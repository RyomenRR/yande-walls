#!/usr/bin/env python3
import os
import sys
import time
from main import (
    ensure_helper_process,
    run_wallpaper,
    set_allow_downloads,
    start_slideshow_timer,
    stop_slideshow_timer,
    reset_next_wallpaper_timer,
    SLIDESHOW_MINUTES,
    log,
)


FORCE_DOWNLOADS = os.environ.get("FORCE_DOWNLOADS", "").lower() in {"1", "true", "yes"}


def main_entry() -> int:
    set_allow_downloads(FORCE_DOWNLOADS)
    ensure_helper_process()
    reset_next_wallpaper_timer()
    if SLIDESHOW_MINUTES > 0:
        interval = max(1, SLIDESHOW_MINUTES) * 60
        try:
            while True:
                code = run_wallpaper()
                stop_event, timer_thread = start_slideshow_timer(interval)
                try:
                    time.sleep(interval)
                finally:
                    stop_slideshow_timer(stop_event, timer_thread)
        except KeyboardInterrupt:
            log("Slideshow interrupted by user")
            return 0
    return run_wallpaper()


if __name__ == "__main__":
    sys.exit(main_entry())
