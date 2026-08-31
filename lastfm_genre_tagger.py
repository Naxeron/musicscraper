#!/usr/bin/env python3
"""
Backward-compatibility shim for lastfm_genre_tagger.py.
Delegates to musicscraper.services.tagger.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from musicscraper.clients.lastfm import LastFMClient, GenreNormalizer
from musicscraper.services.tagger import GenreTaggerService
from musicscraper.cli.main import main as cli_main


def main():
    args = sys.argv[1:]
    return cli_main(["tag", *args])


if __name__ == "__main__":
    sys.exit(main())
