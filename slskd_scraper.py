#!/usr/bin/env python3
"""
Backward-compatibility shim for slskd_scraper.py.
Delegates to musicscraper.services.soulseek.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from musicscraper.services.soulseek import (
    SlskdArtistScraper,
    CandidateFile,
    CandidateDir,
    PeerCandidateIndex,
    is_track_title_match_fast,
    is_dir_name_match_fast,
)
from musicscraper.cli.main import main as cli_main


def main():
    args = sys.argv[1:]
    return cli_main(["soulseek", *args])


if __name__ == "__main__":
    sys.exit(main())
