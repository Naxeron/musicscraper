#!/usr/bin/env python3
"""
Backward-compatibility shim for bandcamp_scraper.py.
Delegates to musicscraper.scrapers.bandcamp.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from musicscraper.scrapers.bandcamp import BandcampEngine
from musicscraper.cli.main import main as cli_main


def main():
    args = sys.argv[1:]
    return cli_main(["bandcamp", *args])


if __name__ == "__main__":
    sys.exit(main())
