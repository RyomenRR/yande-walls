#!/usr/bin/env python3
import sys
from main import download_helper_main, set_allow_downloads


def main_entry() -> int:
    set_allow_downloads(True)
    return download_helper_main()


if __name__ == "__main__":
    sys.exit(main_entry())
