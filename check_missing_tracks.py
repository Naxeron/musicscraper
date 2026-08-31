#!/usr/bin/env python3
"""
Backward-compatibility shim for check_missing_tracks.py.
Delegates to musicscraper.services.auditor and musicscraper.cli.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from musicscraper.config import Config
from musicscraper.core.constants import AUDIO_EXTENSIONS, DEFAULT_CACHE_DIR, GENERIC_OR_COMMON_WORDS
from musicscraper.core.text import (
    normalize_text,
    calculate_similarity,
    parse_track_title_structure,
    are_versions_compatible,
    strip_track_number_and_artist,
    kanji_to_arabic,
)
from musicscraper.clients.musicbrainz import MusicBrainzClient, ArtistCatalog
from musicscraper.clients.navidrome import NavidromeScanner
from musicscraper.services.reconciler import DiscographyReconciler, deduplicate_candidate_tracks
from musicscraper.services.auditor import AudioFileScanner, AuditorService, is_distinct_track_title
from musicscraper.cli.main import main as cli_main


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        return cli_main(["audit", *args])
    return cli_main(["audit", *args])


if __name__ == "__main__":
    sys.exit(main())
